"""Nightly self-improvement loop.

Sleeps until ZORO_REFLECT_HOUR (default 23, local time), then:

  1. Pulls today's open lessons and last 7 days of tool stats.
  2. Groups lessons by `tool` and frequency, picks the top 3.
  3. Asks Claude Haiku to produce a short spoken-friendly paragraph
     summarising the day: what went well, what stumbled, what she'd
     like to improve, and any resources she still needs from Skhathi.
  4. Writes one row to `zoro_daily_reports` (idempotent per date).
  5. Marks the included lessons as `status='reported'`.

Run as a managed process under zoro_supervisor (already registered).
Can also be invoked directly for a one-off cycle:

    python -c "import sys; sys.path.insert(0,'src'); \
        import zoro_reflector; zoro_reflector.run_once()"
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import httpx

import zoro_episodes
import zoro_lessons

_REFLECT_HOUR = int(os.environ.get("ZORO_REFLECT_HOUR", "23"))
_REFLECT_MODEL = "claude-haiku-4-5"
_LLM_TIMEOUT = 30
_DB_TIMEOUT = 8

_SYSTEM_PROMPT = (
    "You are Zoro, a personal voice assistant, writing a short nightly "
    "self-improvement report TO yourself in Skhathi's voice (he is your "
    "principal). The report will be SPOKEN aloud in the morning, so it "
    "must be plain prose: no markdown, no bullets, no em-dashes, no "
    "ellipses. Two to four short sentences total.\n\n"
    "Structure: (1) one sentence on the day's pattern (what worked, "
    "what struggled); (2) one sentence naming the top one or two things "
    "you would improve about yourself and how (be concrete — name a "
    "module or behaviour); (3) if there are missing resources, one "
    "sentence asking Skhathi for them by name; otherwise omit. Begin "
    "naturally (e.g. \"Today I...\"). End cleanly — do not summarise "
    "yourself."
)


def _llm_summary(payload: dict) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    user_msg = (
        "Stats and lessons from the day are below. Write the report.\n\n"
        + json.dumps(payload, indent=2)
    )
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _REFLECT_MODEL,
                "max_tokens": 350,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=_LLM_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        ).strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


def _episode_stats() -> dict[str, int]:
    """Coarse counts of today's episodes by outcome.

    Pulls from tool_stats for the last 1 day — that already aggregates
    success/error/aborted, which is what the report needs.
    """
    rows = zoro_episodes.tool_stats(days=1)
    agg: dict[str, int] = {"success": 0, "error": 0, "aborted": 0}
    for r in rows:
        agg["success"] += r.get("success", 0)
        agg["error"] += r.get("error", 0)
        agg["aborted"] += r.get("aborted", 0)
    return agg


def _pick_top_lessons(lessons: list[dict], limit: int = 3) -> list[dict]:
    """Frequency-weighted: lessons whose `tool` appears more often in
    today's open lessons rank higher; ties broken by recency."""
    if not lessons:
        return []
    tool_freq = Counter(l.get("tool", "") for l in lessons)
    # Group by tool, take the freshest lesson per tool, sort by frequency.
    by_tool: dict[str, dict] = {}
    for l in lessons:  # lessons are already newest first
        t = l.get("tool", "")
        if t not in by_tool:
            by_tool[t] = l
    ranked = sorted(
        by_tool.values(),
        key=lambda l: (-tool_freq[l.get("tool", "")], -(l.get("id") or 0)),
    )
    return ranked[:limit]


def _existing_report_for_today() -> dict | None:
    base = zoro_episodes._base()  # reuse env wiring
    if base is None:
        return None
    url, headers = base
    today = datetime.now().date().isoformat()
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_daily_reports"
            f"?report_date=eq.{today}&select=id,summary,spoken&limit=1",
            headers=headers, timeout=_DB_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json() or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def _write_report(payload: dict) -> int | None:
    base = zoro_episodes._base()
    if base is None:
        return None
    url, headers = base
    # Upsert: one report per date. PostgREST `Prefer: resolution=merge-duplicates`
    # with the unique constraint on report_date handles re-runs the same day.
    try:
        r = httpx.post(
            f"{url}/rest/v1/zoro_daily_reports?select=id",
            headers={
                **headers,
                "Prefer": "return=representation,resolution=merge-duplicates",
            },
            json=payload,
            timeout=_DB_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows, list):
            return int(rows[0]["id"])
    except Exception:  # noqa: BLE001
        return None
    return None


def run_once() -> dict[str, Any]:
    """Generate today's report. Returns a small diagnostic dict so
    callers can log what happened. Safe to call repeatedly; the DB
    upsert keeps it one row per date."""
    open_lessons = zoro_lessons.open_lessons(limit=50)
    stats = _episode_stats()
    top = _pick_top_lessons(open_lessons, limit=3)
    needs = sorted({
        l.get("needs_resource") for l in open_lessons
        if l.get("needs_resource")
    })

    if stats["success"] + stats["error"] + stats["aborted"] == 0 and not top:
        return {"status": "idle", "reason": "no episodes or lessons today"}

    llm_payload = {
        "episode_stats": stats,
        "top_lessons": [
            {
                "tool": l.get("tool"),
                "failure_summary": l.get("failure_summary"),
                "lesson": l.get("lesson"),
                "suggested_improvement": l.get("suggested_improvement"),
            }
            for l in top
        ],
        "needs_resources": needs,
    }
    summary = _llm_summary(llm_payload)
    if not summary:
        return {"status": "skipped", "reason": "no summary (no API key or LLM failure)"}

    payload = {
        "report_date": datetime.now().date().isoformat(),
        "summary": summary,
        "top_lesson_ids": [l.get("id") for l in top if l.get("id")],
        "needs_resources": needs,
        "episode_stats": stats,
        "spoken": False,
    }
    rid = _write_report(payload)
    if rid is None:
        return {"status": "skipped", "reason": "report write failed"}

    for l in top:
        lid = l.get("id")
        if lid:
            zoro_lessons.mark_status(int(lid), "reported")

    return {
        "status": "wrote",
        "report_id": rid,
        "lessons_reported": len(top),
        "needs_resources": needs,
    }


# --- Read side -------------------------------------------------------------


def unspoken_report() -> dict | None:
    """Most recent unspoken report. Morning briefing fetches this."""
    base = zoro_episodes._base()
    if base is None:
        return None
    url, headers = base
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_daily_reports"
            f"?spoken=is.false&order=report_date.desc&limit=1",
            headers=headers, timeout=_DB_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json() or []
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None


def mark_spoken(report_id: int) -> bool:
    base = zoro_episodes._base()
    if base is None or not report_id:
        return False
    url, headers = base
    try:
        r = httpx.patch(
            f"{url}/rest/v1/zoro_daily_reports?id=eq.{int(report_id)}",
            headers={**headers, "Prefer": "return=minimal"},
            json={
                "spoken": True,
                "spoken_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout=_DB_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False


# --- Loop (process entrypoint) --------------------------------------------


def _seconds_until_hour(hour: int) -> float:
    from datetime import timedelta
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


def main() -> None:
    print(
        f"[reflector] starting; will reflect daily at {_REFLECT_HOUR:02d}:00 local",
        flush=True,
    )
    while True:
        wait = _seconds_until_hour(_REFLECT_HOUR)
        print(f"[reflector] sleeping {int(wait)}s until next reflection", flush=True)
        time.sleep(wait)
        try:
            out = run_once()
            print(f"[reflector] {out}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[reflector] cycle failed: {e}", flush=True)
        # Sleep an hour past the slot so we don't double-fire if the
        # cycle finished quickly (sleep loop wakes immediately after).
        time.sleep(3600)


if __name__ == "__main__":
    main()
