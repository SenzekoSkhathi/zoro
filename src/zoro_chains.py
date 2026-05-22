"""Sequence miner — turn recurring tool-call chains into open lessons.

The existing reflector groups lessons by *tool*, which makes 3-step
workflows invisible: a chain of describe_screen → press_keys → type_text
that recurs across many sessions shows up as three independent tool
counts, not as one repeating pattern worth wrapping.

This module bridges that gap. It reads recent episodes, groups by
session, extracts n-gram tool sequences, and writes one open lesson per
candidate chain (n>=3, occurring in 2+ distinct sessions). The lesson
text suggests building a sub-agent for the chain so the existing
reflection → autonomous coder loop can act on it.

Designed to be called once a day (from zoro_reflector.run_once before
the lesson pull). Idempotent: a chain that already has an open lesson
is not re-inserted.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

import zoro_episodes

_TIMEOUT = 8

# n-gram sizes to mine. 3 and 4 catch most useful workflows without
# generating thousands of low-signal candidates.
_NGRAM_SIZES = (3, 4)

# A chain qualifies as recurring if EITHER condition holds:
#   - appears in >= _MIN_DISTINCT_SESSIONS distinct sessions (cross-session
#     recurrence — the strongest signal), OR
#   - shows up >= _MIN_TOTAL_OCCURRENCES times overall (catches intra-session
#     patterns in long working sessions, where the user repeats a shape
#     several times within one conversation).
_MIN_DISTINCT_SESSIONS = 2
_MIN_TOTAL_OCCURRENCES = 3

# Tool names that pollute chains: voice plumbing, status checks, end-of-
# turn cleanup. Filtered out before n-gramming so they don't fragment
# real workflow chains.
_NOISE_TOOLS = frozenset({
    "false_interruption",
    "get_current_time",
    "where_am_i",
    "close_widget",
    "show_chat_history",
})


def _fetch_recent(days: int) -> list[dict]:
    base = zoro_episodes._base()
    if base is None:
        return []
    url, headers = base
    cutoff = quote(
        (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    )
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_episodes"
            f"?select=session_id,tool,created_at,outcome"
            f"&created_at=gte.{cutoff}"
            f"&order=session_id.asc,created_at.asc"
            f"&limit=5000",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception:  # noqa: BLE001
        return []


def _sessions_to_chains(
    rows: list[dict],
) -> dict[tuple[str, ...], dict]:
    """Bucket rows by session_id, drop noise, extract n-grams.

    Returns: { chain_tuple: {"sessions": set[sid], "count": int}, ... }
    """
    by_session: dict[str, list[str]] = {}
    for row in rows:
        sid = row.get("session_id") or "anon"
        tool = row.get("tool", "")
        if not tool or tool in _NOISE_TOOLS:
            continue
        # Phase 3: only consider successful steps when looking for skill
        # candidates. Repeating a chain of failures isn't a skill — it's
        # a bug pattern, which the existing lesson layer already covers.
        if row.get("outcome") not in ("success",):
            continue
        by_session.setdefault(sid, []).append(tool)

    chains: dict[tuple[str, ...], dict] = {}
    for sid, tools in by_session.items():
        # Collapse same-tool runs ("play_music, play_music, play_music")
        # to a single entry — repeated identical calls are usually retries
        # or refinements, not a learnable shape.
        collapsed = [t for i, t in enumerate(tools) if i == 0 or t != tools[i - 1]]
        for n in _NGRAM_SIZES:
            for i in range(len(collapsed) - n + 1):
                key = tuple(collapsed[i : i + n])
                bucket = chains.setdefault(key, {"sessions": set(), "count": 0})
                bucket["sessions"].add(sid)
                bucket["count"] += 1
    return chains


def _signature(chain: tuple[str, ...]) -> str:
    """Stable identifier for a chain, embedded in the lesson task so
    re-mining doesn't duplicate rows."""
    return "chain:" + ">".join(chain)


def _existing_chain_lessons() -> set[str]:
    """Set of signatures already represented in zoro_lessons (any
    status). Prevents the daily miner from spamming duplicates."""
    base = zoro_episodes._base()
    if base is None:
        return set()
    url, headers = base
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_lessons"
            f"?select=task&tool=eq.chain&limit=2000",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return {row.get("task", "") for row in (r.json() or [])}
    except Exception:  # noqa: BLE001
        return set()


def _write_chain_lesson(
    chain: tuple[str, ...], session_count: int, total_count: int,
) -> int | None:
    """Insert a lesson row describing a recurring chain."""
    base = zoro_episodes._base()
    if base is None:
        return None
    url, headers = base
    sig = _signature(chain)
    arrow = " → ".join(chain)
    where = (
        f"{session_count} sessions"
        if session_count >= _MIN_DISTINCT_SESSIONS
        else f"{total_count} times in 1 session"
    )
    payload = {
        "tool": "chain",
        "task": sig,
        "outcome": "pattern",  # not error/aborted — this is a positive pattern
        "failure_summary": f"Recurring chain seen in {where}",
        "lesson": (
            f"Next time you start with {arrow}, treat it as one named "
            "step instead of re-discovering it call-by-call."
        ),
        "suggested_improvement": (
            f"Create a sub-agent (or wrap into a single tool) that "
            f"executes the chain {arrow} as one action. Use build_sub_agent "
            f"with a focused description matching this workflow."
        ),
        "needs_resource": None,
        "status": "open",
    }
    try:
        r = httpx.post(
            f"{url}/rest/v1/zoro_lessons?select=id",
            headers={**headers, "Prefer": "return=representation"},
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json() or []
        return int(rows[0]["id"]) if rows else None
    except Exception:  # noqa: BLE001
        return None


def mine_and_write(days: int = 7, max_new: int = 5) -> dict:
    """Mine recent episodes for recurring chains and write new lesson
    rows for any that don't yet have one. Returns a small diagnostic
    dict for run_once() telemetry."""
    rows = _fetch_recent(days)
    if not rows:
        return {"status": "idle", "reason": "no episodes"}
    chains = _sessions_to_chains(rows)
    candidates: list[tuple[tuple[str, ...], int, int]] = [
        (chain, len(b["sessions"]), b["count"])
        for chain, b in chains.items()
        if len(b["sessions"]) >= _MIN_DISTINCT_SESSIONS
        or b["count"] >= _MIN_TOTAL_OCCURRENCES
    ]
    if not candidates:
        return {"status": "idle", "reason": "no recurring chains"}
    # Rank: distinct-session signal first (stronger), then total count,
    # then chain length (longer = more specific). max_new caps the
    # daily flood when many candidates qualify on the same day.
    candidates.sort(key=lambda c: (-c[1], -c[2], -len(c[0])))

    existing = _existing_chain_lessons()
    written = 0
    written_chains: list[str] = []
    for chain, session_count, total_count in candidates:
        if written >= max_new:
            break
        sig = _signature(chain)
        if sig in existing:
            continue
        lid = _write_chain_lesson(chain, session_count, total_count)
        if lid is not None:
            written += 1
            written_chains.append(sig)
    return {
        "status": "wrote" if written else "idle",
        "candidates_total": len(candidates),
        "written": written,
        "chains": written_chains,
    }


if __name__ == "__main__":
    # Dry-run helper: prints what would be mined without writing.
    import json
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    rows = _fetch_recent(7)
    chains = _sessions_to_chains(rows)
    qual = [
        (c, b) for c, b in chains.items()
        if len(b["sessions"]) >= _MIN_DISTINCT_SESSIONS
        or b["count"] >= _MIN_TOTAL_OCCURRENCES
    ]
    qual.sort(key=lambda x: (-len(x[1]["sessions"]), -x[1]["count"], -len(x[0])))
    print(f"Episodes pulled: {len(rows)}; sessions: "
          f"{len({r.get('session_id') for r in rows})}")
    print(f"Qualifying chains: {len(qual)}")
    for chain, b in qual[:20]:
        print(f"  sessions={len(b['sessions'])} total={b['count']:>2}  "
              f"{' → '.join(chain)}")
