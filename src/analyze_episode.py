"""
Run the four-lens analysis on each episode:
  1. Key takeaways & quotes  (with timestamps when segments are available)
  2. Topics / themes (for trend tracking)
  3. Guests & recurring people
  4. Tone / sentiment

Uses Groq (OpenAI-compatible API) with Llama 3.3 70B by default.
Output is written back into data/episodes/<id>.json under an `analysis` key.

When `transcript_segments` is present, we feed the LLM a timestamped
transcript and ask it to tag each takeaway and quote with the approximate
timestamp_seconds where it occurred - enabling "> jump to moment" links.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import requests


def _post_with_retry(
    request_fn: Callable[[], requests.Response],
    *,
    max_attempts: int = 5,
    backoff_base: float = 15.0,
    label: str = "request",
) -> requests.Response:
    """
    Call request_fn() (which must return a requests.Response) and retry on
    429 rate limits or 5xx errors. Respects the Retry-After header when the
    server sets it, otherwise uses exponential backoff with a small jitter.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            r = request_fn()
        except requests.exceptions.RequestException as e:
            if attempt == max_attempts:
                raise
            wait = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 2)
            print(
                f"[retry] {label}: network error {e} "
                f"(attempt {attempt}/{max_attempts}), sleeping {wait:.1f}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            if attempt == max_attempts:
                return r  # caller's raise_for_status() will surface it
            retry_after = (r.headers.get("Retry-After") or "").strip()
            wait = backoff_base * (2 ** (attempt - 1))
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    pass
            wait += random.uniform(0, 2)
            print(
                f"[retry] {label}: HTTP {r.status_code} "
                f"(attempt {attempt}/{max_attempts}), sleeping {wait:.1f}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        return r
    raise RuntimeError("unreachable")

ROOT = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT / "data" / "episodes"
TRENDS_PATH = ROOT / "data" / "trends.json"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

MAX_TRANSCRIPT_CHARS = 20_000

# Target chunk size for chunked analysis. Stays below MAX_TRANSCRIPT_CHARS so
# each chunk plus its title/description prefix fits under the per-request body
# cap that Groq's free tier enforces.
CHUNK_TARGET_CHARS = 18_000

# --- Prompt ------------------------------------------------------------------

ANALYSIS_PROMPT = """You are analyzing an episode of the All-In Podcast.

Return a single JSON object with exactly these fields:

{
  "summary": "2-3 sentence overview of what the episode covered",
  "takeaways": [
    {
      "text": "A crisp, self-contained insight",
      "timestamp_seconds": <int or null>
    }
  ],
  "quotes": [
    {
      "speaker": "name or 'Unknown'",
      "text": "memorable line, verbatim if possible",
      "timestamp_seconds": <int or null>
    }
  ],
  "topics": ["5-15 short topic tags, lowercase kebab-case"],
  "guests": ["full names of any guests beyond the core hosts"],
  "sentiment": {
    "overall": "optimistic | cautious | bearish | mixed",
    "notes": "1-2 sentences on the mood and why"
  },
  "notable_moments": [
    {"timestamp_seconds": <int or null>, "description": "what happened"}
  ],
  "notable_mentions": [
    "short strings naming any company, executive, product, book, or organization mentioned in the episode, even briefly - e.g. 'Matthew Prince (Cloudflare)', 'OpenAI', 'GPT-5', 'Shyam Sankar (Palantir)'"
  ]
}

Rules:
- 5 to 8 takeaways.
- AT LEAST 2 of the takeaways MUST reference specific named people, companies, or organizations by name (not just abstract themes). For example, prefer "Matthew Prince announced 20% Cloudflare layoffs, arguing AI made 'measurers' redundant" over the generic "AI is causing layoffs at tech companies." Specific names + what they said or did beats abstract patterns. This is critical because downstream search depends on names appearing in the takeaways.
- Where a takeaway summarizes a host's view of someone, include both the host and the subject by name (e.g. "Chamath argued that Dario's doom rhetoric correlates with Anthropic's compute constraints").
- Core hosts (Chamath Palihapitiya, Jason Calacanis, David Sacks, David Friedberg) are NOT guests.
- Keep quotes under 30 words.
- Topic tags should be reusable (prefer "ai-safety" over "sam-altman-warned-about-x").
- notable_mentions: 10 to 30 entries. Include every company, executive, product, book, or organization that was discussed or even mentioned in passing. Each entry should be short (1-6 words). This field powers user search, so be inclusive rather than selective. Format as "Person Name (Org)" when both are known, otherwise just the name.
- If you are given a timestamped transcript (each line prefixed with [seconds]),
  use the nearest matching line's second count for timestamp_seconds. Otherwise
  set timestamp_seconds to null.
- Return ONLY the JSON object. No markdown fences, no preamble.
"""


# Per-chunk prompt for episodes too long to analyze in a single call.
# Each chunk is processed independently, then a merge step combines the outputs.
CHUNK_PROMPT = """You are analyzing CHUNK __PART__ OF __TOTAL__ of an All-In Podcast episode transcript.

This is a partial view of a longer episode. Return a JSON object describing what THIS chunk covers:

{
  "mini_summary": "1-2 sentences on what this chunk discussed",
  "takeaways": [
    {"text": "insight from this chunk", "timestamp_seconds": <int or null>}
  ],
  "quotes": [
    {"speaker": "name or 'Unknown'", "text": "memorable line", "timestamp_seconds": <int or null>}
  ],
  "notable_moments": [
    {"timestamp_seconds": <int or null>, "description": "what happened"}
  ],
  "topics_mentioned": ["short kebab-case tags"],
  "guests_mentioned": ["full names of any guests beyond core hosts"],
  "notable_mentions": ["short strings naming any company, exec, product, book, or org mentioned in THIS chunk"]
}

Rules:
- AT MOST 4 takeaways from THIS chunk only.
- When the chunk contains a memorable concrete example involving a named person, company, or organization, INCLUDE it as a takeaway with the name(s) explicitly in the text. Specifics beat themes.
- AT MOST 4 quotes from THIS chunk only.
- notable_mentions: list every company, exec, product, book, or org mentioned in THIS chunk, even briefly. Format "Person (Org)" when both are known, else just the name. Up to 15 entries.
- Keep quotes under 30 words.
- Core hosts (Chamath Palihapitiya, Jason Calacanis, David Sacks, David Friedberg) are NOT guests.
- If lines are prefixed with [seconds], use the nearest line's seconds for timestamp_seconds.
- Return ONLY the JSON object. No markdown, no preamble.
"""


# Merge prompt: combines multiple chunk outputs into the final analysis JSON
# in the same schema as ANALYSIS_PROMPT so downstream code is unaffected.
MERGE_PROMPT = """You are merging the analyses of multiple chunks of a single All-In Podcast episode into one unified analysis.

Input is a JSON object: {"title", "description", "chunks": [chunk_outputs...]}.

Produce ONE JSON object in this exact schema:

{
  "summary": "2-3 sentence overview of the WHOLE episode",
  "takeaways": [
    {"text": "insight", "timestamp_seconds": <int or null>}
  ],
  "quotes": [
    {"speaker": "name or 'Unknown'", "text": "memorable line", "timestamp_seconds": <int or null>}
  ],
  "topics": ["5-15 short kebab-case tags"],
  "guests": ["full names of any guests beyond core hosts"],
  "sentiment": {
    "overall": "optimistic | cautious | bearish | mixed",
    "notes": "1-2 sentences on the mood and why"
  },
  "notable_moments": [
    {"timestamp_seconds": <int or null>, "description": "what happened"}
  ],
  "notable_mentions": [
    "consolidated list of companies, execs, products, books, orgs mentioned anywhere in the episode"
  ]
}

Rules:
- Pick the 5-8 BEST takeaways across all chunks (dedupe near-duplicates).
- AT LEAST 2 of the chosen takeaways MUST mention specific named people, companies, or organizations (not just abstract themes). For example, prefer "Matthew Prince announced 20% Cloudflare layoffs because AI made 'measurers' redundant" over the generic "AI is causing layoffs." This is critical for downstream search.
- When a chunk had a specific named-example takeaway, prefer keeping it over a more abstract one from another chunk.
- notable_mentions: union of all chunks' notable_mentions, deduplicated. Aim for 15-30 entries. Be inclusive - this powers user search.
- Pick the 5-10 best quotes (preserve their original timestamp_seconds).
- Topics should consolidate across all chunks; remove duplicates.
- Core hosts (Chamath Palihapitiya, Jason Calacanis, David Sacks, David Friedberg) are NOT guests.
- Sentiment is a holistic read of the entire episode, not any single chunk.
- Return ONLY the JSON object. No markdown, no preamble.
"""


def _format_transcript(ep: dict[str, Any]) -> str:
    """Prefer timestamped segments when available so the LLM can tag moments."""
    segments = ep.get("transcript_segments") or []
    if segments:
        lines = []
        for s in segments:
            start = int(s.get("start", 0))
            text = (s.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"[{start}] {text}")
        return "\n".join(lines)
    return ep.get("transcript", "") or ""


def call_groq(prompt: str, body: str, api_key: str) -> dict[str, Any]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": body[:MAX_TRANSCRIPT_CHARS]},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    r = _post_with_retry(
        lambda: requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=180,
        ),
        label="groq.chat",
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def _split_into_chunks(transcript: str, target_size: int = CHUNK_TARGET_CHARS) -> list[str]:
    """Split a formatted transcript into chunks of up to ``target_size`` chars.

    Prefers line boundaries so timestamp prefixes stay intact. Returns a single
    chunk if the transcript already fits in ``target_size``.
    """
    if len(transcript) <= target_size:
        return [transcript]
    lines = transcript.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        line_size = len(line) + 1  # +1 for the newline separator
        if current_size + line_size > target_size and current:
            chunks.append("\n".join(current))
            current = [line]
            current_size = line_size
        else:
            current.append(line)
            current_size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def _analyze_chunked(
    title: str,
    description: str,
    transcript: str,
    ep_id: str,
    api_key: str,
) -> dict[str, Any]:
    """Process a long transcript by chunking and merging.

    Each chunk is analyzed independently. The chunk outputs are then sent to
    the model with a merge prompt that produces the final analysis JSON in
    the same schema as the single-call path.
    """
    chunks = _split_into_chunks(transcript)
    chunk_outputs: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        prompt = (
            CHUNK_PROMPT
            .replace("__PART__", str(i + 1))
            .replace("__TOTAL__", str(len(chunks)))
        )
        body = (
            f"Episode title: {title}\n\n"
            f"Description: {description}\n\n"
            f"Transcript chunk {i+1} of {len(chunks)} (timestamps in seconds):\n{chunk}"
        )
        result = call_groq(prompt, body, api_key)
        chunk_outputs.append(result)
        print(f"[chunk] {ep_id}: {i+1}/{len(chunks)} done", file=sys.stderr)

    merge_input = json.dumps(
        {"title": title, "description": description, "chunks": chunk_outputs},
        ensure_ascii=False,
    )
    final = call_groq(MERGE_PROMPT, merge_input, api_key)
    final["chunk_count"] = len(chunks)
    final["transcript_truncated"] = False
    return final


def analyze_one(ep: dict[str, Any], api_key: str) -> dict[str, Any]:
    title = ep.get("title", "")
    description = ep.get("description", "")
    transcript = _format_transcript(ep)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        result = _analyze_chunked(title, description, transcript, ep.get("id", "?"), api_key)
    else:
        text = (
            f"Episode title: {title}\n\n"
            f"Description: {description}\n\n"
            f"Transcript (timestamps in seconds, when available):\n{transcript}"
        )
        result = call_groq(ANALYSIS_PROMPT, text, api_key)
        result["chunk_count"] = 1
        result["transcript_truncated"] = False
    # Light normalization - earlier versions returned takeaways as strings.
    result["takeaways"] = _normalize_items(result.get("takeaways", []))
    result["quotes"] = _normalize_quotes(result.get("quotes", []))
    result["notable_mentions"] = _normalize_strings(result.get("notable_mentions", []))
    return result


def _normalize_strings(items: list[Any]) -> list[str]:
    """Coerce notable_mentions into a list of clean strings, dedup case-insensitively."""
    seen = set()
    out = []
    for it in items:
        if isinstance(it, str):
            s = it.strip()
        elif isinstance(it, dict):
            # Some LLM outputs wrap entries as {"name": "...", "org": "..."} - flatten.
            name = (it.get("name") or it.get("text") or "").strip()
            org = (it.get("org") or it.get("organization") or "").strip()
            s = f"{name} ({org})" if name and org else name or org
        else:
            continue
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _normalize_items(items: list[Any]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"text": it, "timestamp_seconds": None})
        elif isinstance(it, dict):
            out.append(
                {
                    "text": it.get("text", ""),
                    "timestamp_seconds": it.get("timestamp_seconds"),
                }
            )
    return out


def _normalize_quotes(items: list[Any]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(
                {
                    "text": it.get("text", ""),
                    "speaker": it.get("speaker", "Unknown"),
                    "timestamp_seconds": it.get("timestamp_seconds"),
                }
            )
    return out


# --- Trends ------------------------------------------------------------------

def update_trends(all_eps: list[dict[str, Any]]) -> dict[str, Any]:
    topic_counts: Counter[str] = Counter()
    topic_timeline: dict[str, list[str]] = defaultdict(list)
    guest_counts: Counter[str] = Counter()
    sentiment_timeline: list[dict[str, str]] = []

    for ep in sorted(all_eps, key=lambda e: e.get("date", "")):
        a = ep.get("analysis") or {}
        for t in a.get("topics", []):
            topic_counts[t] += 1
            topic_timeline[t].append(ep["date"])
        for g in a.get("guests", []):
            guest_counts[g] += 1
        sent = a.get("sentiment", {}) or {}
        if sent.get("overall"):
            sentiment_timeline.append(
                {"date": ep["date"], "overall": sent["overall"]}
            )

    trends = {
        "top_topics": topic_counts.most_common(25),
        "topic_timeline": {k: v for k, v in topic_timeline.items()},
        "top_guests": guest_counts.most_common(25),
        "sentiment_timeline": sentiment_timeline,
        "episode_count": len(all_eps),
    }
    TRENDS_PATH.write_text(json.dumps(trends, indent=2, ensure_ascii=False))
    return trends


def run(force: bool = False) -> None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: GROQ_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Process newest episodes first so rate limits eat the oldest backlog
    # (which users care about least), not the most recent episodes.
    episode_files = sorted(EPISODES_DIR.glob("*.json"), reverse=True)
    all_eps: list[dict[str, Any]] = []

    for path in episode_files:
        ep = json.loads(path.read_text())
        if ep.get("analysis") and not force:
            all_eps.append(ep)
            continue
        if not (ep.get("transcript") or ep.get("transcript_segments")):
            print(f"[skip] {ep['id']}: no transcript available", file=sys.stderr)
            all_eps.append(ep)
            continue
        try:
            ep["analysis"] = analyze_one(ep, api_key)
            path.write_text(json.dumps(ep, indent=2, ensure_ascii=False))
            print(f"[analyzed] {ep['id']}")
        except Exception as e:
            print(f"[fail] {ep['id']}: {e}", file=sys.stderr)
        all_eps.append(ep)

    update_trends(all_eps)
    print(f"[trends] updated with {len(all_eps)} episodes")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="Re-analyze every episode.")
    args = p.parse_args()
    run(force=args.force)
