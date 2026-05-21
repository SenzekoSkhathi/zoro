"""YouTube access: search + transcript fetch + Claude-powered summary.

Search uses the YouTube Data API v3 (needs YOUTUBE_API_KEY in .env).
Transcripts use the youtube-transcript-api library (no auth).
Summarise hits the Anthropic API directly (ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import os
import re
import webbrowser
from typing import Optional

import httpx

API_BASE = "https://www.googleapis.com/youtube/v3"
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
# Cap transcript text we send to Claude — long lectures can hit 100K+ chars
# which costs tokens and adds latency. ~24K chars ≈ ~6K tokens, plenty for
# a one-paragraph summary.
MAX_SUMMARY_CHARS = 24_000


def _api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        raise RuntimeError(
            "YOUTUBE_API_KEY is not set. Enable YouTube Data API v3 "
            "in your Google Cloud project and add the key to .env."
        )
    return key


def _extract_video_id(query_or_url: str) -> Optional[str]:
    """If `query_or_url` looks like a YouTube URL or bare 11-char ID,
    pull out the video id. Otherwise return None."""
    s = query_or_url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = re.search(
        r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/|v/))"
        r"([A-Za-z0-9_-]{11})",
        s,
    )
    return m.group(1) if m else None


def search(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube. Returns [{id, title, channel, url, thumbnail}]."""
    params = {
        "key": _api_key(),
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": str(limit),
    }
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{API_BASE}/search", params=params)
        r.raise_for_status()
        data = r.json()
    out: list[dict] = []
    for item in data.get("items", []):
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            continue
        sn = item.get("snippet") or {}
        thumbs = sn.get("thumbnails") or {}
        # medium is 320x180; good enough for cards and small payload.
        thumb = (
            (thumbs.get("medium") or thumbs.get("default") or {})
            .get("url", "")
        )
        out.append(
            {
                "id": vid,
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail": thumb,
            }
        )
    return out


def search_and_open(query: str) -> Optional[dict]:
    """Search; open the top hit in the default browser; return its dict.
    None if nothing matched."""
    results = search(query, limit=1)
    if not results:
        return None
    top = results[0]
    webbrowser.open(top["url"])
    return top


def transcript(query_or_url: str) -> dict:
    """Fetch a video's transcript. `query_or_url` may be a YouTube URL,
    a bare 11-char video id, or a search query (in which case the top
    hit is used).

    Returns {video, text, segments}: `video` is the looked-up metadata,
    `text` is the joined transcript, `segments` is the raw timed list.
    Raises LookupError if no transcript is available.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )

    vid = _extract_video_id(query_or_url)
    video_meta: dict = {}
    if not vid:
        hits = search(query_or_url, limit=1)
        if not hits:
            raise LookupError(
                f"no YouTube video matching {query_or_url!r}"
            )
        video_meta = hits[0]
        vid = video_meta["id"]
    else:
        # Fill in title/channel so the card has something to show.
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(
                    f"{API_BASE}/videos",
                    params={
                        "key": _api_key(),
                        "part": "snippet",
                        "id": vid,
                    },
                )
                if r.status_code == 200:
                    items = (r.json() or {}).get("items", [])
                    if items:
                        sn = items[0].get("snippet") or {}
                        video_meta = {
                            "id": vid,
                            "title": sn.get("title", ""),
                            "channel": sn.get("channelTitle", ""),
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "thumbnail": (
                                (sn.get("thumbnails") or {})
                                .get("medium", {})
                                .get("url", "")
                            ),
                        }
        except Exception:  # noqa: BLE001 - metadata is best-effort
            pass

    try:
        segs = YouTubeTranscriptApi().fetch(vid).to_raw_data()
    except (NoTranscriptFound, TranscriptsDisabled):
        raise LookupError("that video has no transcript available")
    except VideoUnavailable:
        raise LookupError("that video is unavailable")

    text = " ".join(s["text"] for s in segs if s.get("text"))
    return {"video": video_meta, "text": text, "segments": segs}


def summarise(text: str) -> str:
    """Hand `text` to Claude (Haiku) and ask for a one-paragraph summary.

    Truncates very long transcripts so we don't pay for tokens we don't
    need. Returns the summary string.
    """
    import anthropic

    snippet = text[:MAX_SUMMARY_CHARS]
    truncated = len(text) > MAX_SUMMARY_CHARS
    sys_prompt = (
        "You are summarising a YouTube video transcript for a busy "
        "user. Reply with one tight paragraph (4-6 sentences) that "
        "captures the core argument, key facts, and conclusion. No "
        "bullet points, no preamble like 'this video is about'. Just "
        "the summary."
    )
    user = snippet + (
        "\n\n[Transcript truncated for length.]" if truncated else ""
    )
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=600,
        system=sys_prompt,
        messages=[{"role": "user", "content": user}],
    )
    parts = [
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ]
    return "\n".join(parts).strip()
