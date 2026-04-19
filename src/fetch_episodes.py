"""
Fetch All-In Podcast episodes.

Strategy:
1. Pull the podcast RSS feed (Megaphone) for core episode metadata + audio URL.
2. Pull the YouTube channel RSS and match each episode to its YouTube video
   so we have a URL that supports ?t=SECONDS deep links.
3. Try to fetch a posted transcript from allin.com (no timestamps, but fast).
4. If Whisper is enabled, transcribe the audio — returns timestamped segments
   which the analyzer uses to put real "jump to moment" links on takeaways.

Each episode is saved as data/episodes/<episode_id>.json with fields:
    id, number, title, date, url, audio_url, youtube_url, youtube_video_id,
    guests, description, transcript, transcript_segments
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import feedparser
import requests
from bs4 import BeautifulSoup

# ---- Config -----------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT / "data" / "episodes"
EPISODES_DIR.mkdir(parents=True, exist_ok=True)

# Audio RSS (Libsyn is the canonical feed). Falls back through the list on failure.
RSS_CANDIDATES = [
    "https://allinchamathjason.libsyn.com/rss",
    "https://feeds.libsyn.com/121508/rss",
    "https://feeds.megaphone.fm/all-in-with-chamath-jason-sacks-friedberg",
    "https://allin.com/rss",
]

# YouTube channel RSS for @allin. If the channel ID changes, update here or
# set the YOUTUBE_CHANNEL_ID environment variable.
#
# How to find a channel ID: open the channel in a browser, view page source,
# search for "channelId" — it looks like UCxxxxxxxxxxxxxxxxxxxxxx.
DEFAULT_YOUTUBE_CHANNEL_ID = "UCESLZhusAkFfsNsApnjF_Cg"  # All-In Podcast
YOUTUBE_RSS_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

EPISODES_PAGE = "https://allin.com/episodes"

USER_AGENT = "PodcastTracker/1.0 (+https://github.com/yourname/podcast-tracker)"
HEADERS = {"User-Agent": USER_AGENT}

# ---- Helpers ----------------------------------------------------------------

def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "-", text)[:80]


def save_episode(ep: dict[str, Any]) -> Path:
    path = EPISODES_DIR / f"{ep['id']}.json"
    path.write_text(json.dumps(ep, indent=2, ensure_ascii=False))
    return path


def episode_already_saved(episode_id: str) -> bool:
    return (EPISODES_DIR / f"{episode_id}.json").exists()


def _normalize_for_match(s: str) -> str:
    """Used to fuzzy-match podcast RSS titles to YouTube video titles."""
    s = s.lower()
    s = re.sub(r"^e\d+[:\-–\s]+", "", s)   # strip "E220: " prefix
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---- RSS ingest (audio podcast feed) ----------------------------------------

def fetch_from_rss() -> list[dict[str, Any]]:
    feed = None
    for url in RSS_CANDIDATES:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
            if parsed.entries:
                feed = parsed
                break
        except Exception as e:
            print(f"[rss] {url} failed: {e}", file=sys.stderr)
    if not feed:
        raise RuntimeError("No RSS feed reachable — update RSS_CANDIDATES.")

    episodes: list[dict[str, Any]] = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        pub_date = entry.get("published_parsed")
        date_iso = (
            datetime(*pub_date[:6]).date().isoformat() if pub_date else ""
        )
        m = re.match(r"E(\d+)\s*[:\-–]\s*(.*)", title)
        number = int(m.group(1)) if m else None
        clean_title = m.group(2).strip() if m else title

        audio_url = ""
        for enc in entry.get("enclosures", []) or []:
            if enc.get("type", "").startswith("audio"):
                audio_url = enc.get("href", "")
                break

        ep_id = f"{date_iso}-{slugify(clean_title)}" if date_iso else slugify(title)
        episodes.append(
            {
                "id": ep_id,
                "number": number,
                "title": clean_title,
                "date": date_iso,
                "url": entry.get("link", ""),
                "audio_url": audio_url,
                "youtube_url": "",
                "youtube_video_id": "",
                "description": entry.get("summary", ""),
                "guests": _guess_guests(clean_title, entry.get("summary", "")),
                "transcript": "",
                "transcript_segments": [],  # list of {start, end, text}
            }
        )
    return episodes


def _guess_guests(title: str, description: str) -> list[str]:
    m = re.search(r"with\s+([A-Z][\w\.\-]+(?:\s+[A-Z][\w\.\-]+)+)", title)
    if m:
        return [m.group(1).strip()]
    return []


# ---- YouTube URL discovery --------------------------------------------------

def fetch_youtube_index() -> list[dict[str, Any]]:
    """Return list of {title, date, url, video_id} from YouTube channel RSS."""
    cid = os.environ.get("YOUTUBE_CHANNEL_ID") or DEFAULT_YOUTUBE_CHANNEL_ID
    url = YOUTUBE_RSS_TEMPLATE.format(cid=cid)
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"[youtube] rss parse failed: {e}", file=sys.stderr)
        return []
    out = []
    for e in parsed.entries:
        vid = e.get("yt_videoid") or e.get("id", "").split(":")[-1]
        pub = e.get("published_parsed")
        date_iso = datetime(*pub[:6]).date().isoformat() if pub else ""
        out.append(
            {
                "title": e.get("title", ""),
                "date": date_iso,
                "url": e.get("link", ""),
                "video_id": vid,
            }
        )
    return out


def attach_youtube_urls(
    episodes: list[dict[str, Any]], yt_index: list[dict[str, Any]]
) -> None:
    """In-place: match each podcast episode to its YouTube entry by title+date."""
    if not yt_index:
        return
    for ep in episodes:
        best, best_score = None, 0.0
        ep_norm = _normalize_for_match(ep["title"])
        for yt in yt_index:
            yt_norm = _normalize_for_match(yt["title"])
            # Date penalty: YouTube upload often same day or ±2 days from RSS pub.
            date_ok = abs(
                _days_between(ep["date"], yt["date"])
            ) <= 7 if ep["date"] and yt["date"] else True
            score = SequenceMatcher(None, ep_norm, yt_norm).ratio()
            if date_ok and score > best_score:
                best, best_score = yt, score
        # Only accept if the fuzzy-match is confident enough.
        if best and best_score >= 0.55:
            ep["youtube_url"] = best["url"]
            ep["youtube_video_id"] = best["video_id"]


def _days_between(a: str, b: str) -> int:
    try:
        da = datetime.fromisoformat(a).date()
        db = datetime.fromisoformat(b).date()
        return (da - db).days
    except Exception:
        return 0


# ---- Transcript fetching ----------------------------------------------------

def fetch_transcript_from_allin(episode_url: str) -> Optional[str]:
    if not episode_url:
        return None
    try:
        r = requests.get(episode_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[transcript] fetch failed for {episode_url}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    for selector in [
        "[data-testid='transcript']",
        ".transcript",
        "article .prose",
        "main article",
    ]:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) > 500:
            return el.get_text("\n", strip=True)
    return None


# Groq's free Whisper tier caps uploads ~25MB. All-In episodes are 1.5–2 hours
# which is ~100MB at normal podcast quality — too big. We compress to 16kHz mono
# Opus at 24kbps (Whisper's preferred shape) which shrinks a 2-hour file to ~22MB.
# If a show is still too long after compression, we split into chunks and stitch
# the timestamps back together.
GROQ_MAX_UPLOAD_MB = 24
WHISPER_CHUNK_SECONDS = 25 * 60  # 25-minute chunks when splitting


def transcribe_with_groq(
    audio_url: str, api_key: str
) -> tuple[str, list[dict[str, Any]]]:
    """
    Transcribe with Groq Whisper. Returns (full_text, segments).

    Each segment has {start, end, text} — start/end in seconds.
    Used downstream to attach timestamps to takeaways and quotes.
    """
    if not audio_url or not api_key:
        return "", []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        raw = tmp_path / "raw.mp3"
        try:
            audio = requests.get(audio_url, headers=HEADERS, timeout=300).content
            raw.write_bytes(audio)
        except Exception as e:
            print(f"[transcribe] audio download failed: {e}", file=sys.stderr)
            return "", []

        compressed = tmp_path / "compressed.ogg"
        if not _compress_audio(raw, compressed):
            return "", []

        size_mb = compressed.stat().st_size / (1024 * 1024)
        if size_mb <= GROQ_MAX_UPLOAD_MB:
            return _transcribe_file(compressed, api_key, offset=0.0)

        # Still too large — chunk into smaller pieces and stitch.
        return _transcribe_chunked(compressed, api_key, tmp_path)


def _compress_audio(src: Path, dst: Path) -> bool:
    """Shrink audio to 16kHz mono Opus @ 24kbps using ffmpeg."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                "-ac", "1", "-ar", "16000",
                "-c:a", "libopus", "-b:a", "24k",
                str(dst),
            ],
            check=True, capture_output=True, timeout=900,
        )
        return True
    except Exception as e:
        print(f"[transcribe] ffmpeg compress failed: {e}", file=sys.stderr)
        return False


def _audio_duration_seconds(path: Path) -> float:
    """Ask ffprobe how long the audio file is."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception as e:
        print(f"[transcribe] ffprobe failed: {e}", file=sys.stderr)
        return 0.0


def _transcribe_file(
    path: Path, api_key: str, offset: float = 0.0
) -> tuple[str, list[dict[str, Any]]]:
    """Send one audio file to Groq Whisper and shift segment timestamps by offset."""
    try:
        with open(path, "rb") as f:
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (path.name, f, "audio/ogg")},
                data={
                    "model": "whisper-large-v3",
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "segment",
                },
                timeout=900,
            )
        r.raise_for_status()
        data = r.json()
        text = data.get("text", "") or ""
        segments = [
            {
                "start": round(s.get("start", 0) + offset, 2),
                "end": round(s.get("end", 0) + offset, 2),
                "text": s.get("text", "").strip(),
            }
            for s in data.get("segments", [])
        ]
        return text, segments
    except Exception as e:
        print(f"[transcribe] groq failed: {e}", file=sys.stderr)
        return "", []


def _transcribe_chunked(
    compressed: Path, api_key: str, tmp_path: Path
) -> tuple[str, list[dict[str, Any]]]:
    """Split compressed audio into chunks, transcribe each, stitch results."""
    duration = _audio_duration_seconds(compressed)
    if duration <= 0:
        return "", []

    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    t = 0.0
    idx = 0
    while t < duration:
        chunk_path = tmp_path / f"chunk_{idx:03d}.ogg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", str(t),
                    "-t", str(WHISPER_CHUNK_SECONDS),
                    "-i", str(compressed),
                    "-c", "copy",
                    str(chunk_path),
                ],
                check=True, capture_output=True, timeout=600,
            )
        except Exception as e:
            print(f"[transcribe] ffmpeg chunk {idx} failed: {e}", file=sys.stderr)
            break

        text, segs = _transcribe_file(chunk_path, api_key, offset=t)
        if text:
            texts.append(text)
        segments.extend(segs)
        t += WHISPER_CHUNK_SECONDS
        idx += 1

    return " ".join(texts), segments


# ---- Orchestration ----------------------------------------------------------

def run(
    backfill: bool = False,
    limit: Optional[int] = None,
    transcribe_audio: bool = False,
) -> list[dict[str, Any]]:
    groq_key = os.environ.get("GROQ_API_KEY", "")
    episodes = fetch_from_rss()
    if limit:
        episodes = episodes[:limit]

    # Grab YouTube index once; attach video URLs in bulk
    yt_index = fetch_youtube_index()
    print(f"[youtube] loaded {len(yt_index)} videos from channel RSS")
    attach_youtube_urls(episodes, yt_index)
    matched = sum(1 for e in episodes if e["youtube_url"])
    print(f"[youtube] matched {matched}/{len(episodes)} episodes to YouTube videos")

    processed = []
    for ep in episodes:
        if not backfill and episode_already_saved(ep["id"]):
            continue

        # Posted transcripts are faster & cheaper, but don't carry timestamps.
        # If timestamps matter to you, set transcribe_audio=True.
        transcript_text = fetch_transcript_from_allin(ep["url"])
        if transcribe_audio:
            whisper_text, segments = transcribe_with_groq(ep["audio_url"], groq_key)
            ep["transcript"] = whisper_text or transcript_text or ""
            ep["transcript_segments"] = segments
        else:
            ep["transcript"] = transcript_text or ""
            ep["transcript_segments"] = []

        save_episode(ep)
        processed.append(ep)
        print(f"[saved] {ep['id']} (yt={'y' if ep['youtube_url'] else 'n'}, "
              f"segments={len(ep['transcript_segments'])})")
        time.sleep(0.5)

    return processed


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument(
        "--transcribe-audio",
        action="store_true",
        help="Use Groq Whisper (gets timestamps). Recommended if you want "
             "timestamp deep-links in the site.",
    )
    args = p.parse_args()
    run(
        backfill=args.backfill,
        limit=args.limit,
        transcribe_audio=args.transcribe_audio,
    )
