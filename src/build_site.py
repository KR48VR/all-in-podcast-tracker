"""
Generate the static site from the episode JSONs + trends.json.

Writes:
  site/index.html     (checked into the repo; not regenerated)
  site/data.json      (bundled payload the browser reads)
  data/briefs/weekly-<date>.md  (rolling brief, regenerated each run)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT / "data" / "episodes"
TRENDS_PATH = ROOT / "data" / "trends.json"
BRIEFS_DIR = ROOT / "data" / "briefs"
SITE_DIR = ROOT / "site"

BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
SITE_DIR.mkdir(parents=True, exist_ok=True)


def load_episodes() -> list[dict[str, Any]]:
    eps = [json.loads(p.read_text()) for p in EPISODES_DIR.glob("*.json")]
    return sorted(eps, key=lambda e: e.get("date", ""), reverse=True)


# ---- Brief -----------------------------------------------------------------

def _fmt_ts(seconds: Any) -> str:
    try:
        s = int(seconds)
    except Exception:
        return ""
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _ts_link(youtube_url: str, seconds: Any) -> str:
    if not youtube_url or seconds is None:
        return ""
    try:
        s = int(seconds)
    except Exception:
        return ""
    sep = "&" if "?" in youtube_url else "?"
    label = _fmt_ts(seconds)
    return f" [▶ {label}]({youtube_url}{sep}t={s}s)"


def generate_brief(episodes: list[dict[str, Any]], trends: dict) -> str:
    if not episodes:
        return "# Brief\n\n_No episodes yet._"

    latest = episodes[0]
    a = latest.get("analysis", {}) or {}
    yt = latest.get("youtube_url", "")
    top_topics = trends.get("top_topics", [])[:10]
    top_guests = trends.get("top_guests", [])[:5]
    sentiment_timeline = trends.get("sentiment_timeline", [])
    recent_sentiment = sentiment_timeline[-6:] if sentiment_timeline else []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Weekly Brief — {today}",
        "",
        f"**Latest episode:** {latest.get('title', '(untitled)')}"
        f" — {latest.get('date', '')}",
        "",
        "## What this episode covered",
        a.get("summary", "_No summary available_"),
        "",
        "## Top takeaways",
    ]
    for t in a.get("takeaways", []):
        text = t.get("text", "") if isinstance(t, dict) else str(t)
        ts = t.get("timestamp_seconds") if isinstance(t, dict) else None
        lines.append(f"- {text}{_ts_link(yt, ts)}")

    lines += ["", "## Notable quotes"]
    for q in a.get("quotes", [])[:4]:
        text = q.get("text", "")
        speaker = q.get("speaker", "Unknown")
        ts = q.get("timestamp_seconds")
        lines.append(f"> \"{text}\" — {speaker}{_ts_link(yt, ts)}")

    lines += ["", "## Trend check across the whole archive"]
    lines.append("**Most-discussed topics overall:**")
    for topic, count in top_topics:
        lines.append(f"- `{topic}` — mentioned in {count} episodes")

    if top_guests:
        lines += ["", "**Most frequent non-host guests:**"]
        for g, count in top_guests:
            lines.append(f"- {g} ({count} appearance{'s' if count > 1 else ''})")

    if recent_sentiment:
        lines += ["", "**Recent tone trajectory:**"]
        for s in recent_sentiment:
            lines.append(f"- {s['date']}: {s['overall']}")

    lines += ["", "---", f"_Generated {today} from {len(episodes)} episodes_"]
    return "\n".join(lines)


# ---- Payload ---------------------------------------------------------------

def build_payload(episodes: list[dict], trends: dict, brief_md: str) -> dict:
    trimmed = []
    for ep in episodes:
        a = ep.get("analysis", {}) or {}
        trimmed.append(
            {
                "id": ep["id"],
                "number": ep.get("number"),
                "title": ep.get("title"),
                "date": ep.get("date"),
                "url": ep.get("url"),
                "youtube_url": ep.get("youtube_url", ""),
                "youtube_video_id": ep.get("youtube_video_id", ""),
                "summary": a.get("summary", ""),
                "takeaways": a.get("takeaways", []),
                "quotes": a.get("quotes", []),
                "topics": a.get("topics", []),
                "guests": a.get("guests", []),
                "sentiment": a.get("sentiment", {}),
                "notable_moments": a.get("notable_moments", []),
            }
        )
    return {
        "episodes": trimmed,
        "trends": trends,
        "brief_markdown": brief_md,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def run() -> None:
    episodes = load_episodes()
    trends = (
        json.loads(TRENDS_PATH.read_text()) if TRENDS_PATH.exists() else {}
    )
    brief_md = generate_brief(episodes, trends)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (BRIEFS_DIR / f"weekly-{today}.md").write_text(brief_md)

    payload = build_payload(episodes, trends, brief_md)
    (SITE_DIR / "data.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    if not (SITE_DIR / "index.html").exists():
        print("[warn] site/index.html missing", file=sys.stderr)
    print(f"[site] wrote data.json with {len(episodes)} episodes")
    print(f"[site] brief at data/briefs/weekly-{today}.md")


if __name__ == "__main__":
    run()
