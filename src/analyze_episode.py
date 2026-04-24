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
timestamp_seconds where it occurred — enabling "▶ jump to moment" links.
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

MAX_TRANSCRIPT_CHARS = 60_000

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
  ]
}

Rules:
- 5 to 8 takeaways.
- Core hosts (Chamath Palihapitiya, Jason Calacanis, David Sacks, David Friedberg) are NOT guests.
- Keep quotes under 30 words.
- Topic tags should be reusable (prefer "ai-safety" over "sam-altman-warned-about-x").
- If you are given a timestamped transcript (each line prefixed with [seconds]),
  use the nearest matching line's second count for timestamp_seconds. Otherwise
  set timestamp_seconds to null.
- Return ONLY the JSON object. No markdown fences, no preamble.
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


def analyze_one(ep: dict[str, Any], api_key: str) -> dict[str, Any]:
    title = ep.get("title", "")
    description = ep.get("description", "")
    transcript = _format_transcript(ep)
    text = (
        f"Episode title: {title}\n\n"
        f"Description: {description}\n\n"
        f"Transcript (timestamps in seconds, when available):\n{transcript}"
    )
    result = call_groq(ANALYSIS_PROMPT, text, api_key)
    # Light normalization — earlier versions returned takeaways as strings.
    result["takeaways"] = _normalize_items(result.get("takeaways", []))
    result["quotes"] = _normalize_quotes(result.get("quotes", []))
    return result


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

    episode_files = sorted(EPISODES_DIR.glob("*.json"))
    all_eps: list[dict[str, Any]] = []

    for path in episode_files:
        ep = json.loads(path.read_text())
        if ep.get("analysis") and not force:
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
