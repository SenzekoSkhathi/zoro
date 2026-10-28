"""Autonomous self-modification: caps, logging, and the picker.

This module is the policy layer that sits between zoro_reflector (which
decides *when* to think about self-improvement) and zoro_claude_code
(which actually runs the change). It enforces:

  * daily cap: ZORO_SELF_MOD_DAILY_CAP attempts per 24h (default 3)
  * stand-down: when Zoro is stood down, no autonomous attempts
  * kill switch: ZORO_AUTO_SELF_MOD=0 disables autonomous mods entirely
  * size cap: small-change-only heuristic (max files / max lines) — the
    harness already enforces these when passed; here we just choose the
    caps for autonomous runs.

It also logs each attempt to `zoro_self_mod_log` so the morning report
can say "yesterday I tried X — it merged" / "rolled back because Y".
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

import zoro_episodes  # for _base()
import zoro_safety

DEFAULT_DAILY_CAP = 3
# Small-change ceiling for autonomous runs. Anything bigger gets rolled
# back as "proposed-only" so it surfaces in the morning report instead.
DEFAULT_MAX_FILES = 2
DEFAULT_MAX_LINES = 50

_DB_TIMEOUT = 8


def autonomous_enabled() -> bool:
    if os.environ.get("ZORO_AUTO_SELF_MOD", "1") == "0":
        return False
    try:
        if zoro_safety.is_stood_down():
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def daily_cap() -> int:
    try:
        return int(os.environ.get("ZORO_SELF_MOD_DAILY_CAP", DEFAULT_DAILY_CAP))
    except ValueError:
        return DEFAULT_DAILY_CAP


def attempts_in_last_24h() -> int:
    """Count rows written in the last 24h. Returns DEFAULT_DAILY_CAP
    (i.e. blocks autonomous runs) on any DB error — fail closed."""
    if zoro_episodes._pg_url():
        try:
            with zoro_episodes._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT count(*) FROM zoro.zoro_self_mod_log
                        WHERE created_at >= NOW() - INTERVAL '24 hours';
                        """
                    )
                    row = cur.fetchone()
                    if row:
                        val = row["count"] if isinstance(row, dict) else row[0]
                        return int(val)
        except Exception:  # noqa: BLE001
            pass

    base = zoro_episodes._base()
    if base is None:
        return daily_cap()
    url, headers = base
    cutoff = quote(
        (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    )
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_self_mod_log"
            f"?select=id&created_at=gte.{cutoff}",
            headers={**headers, "Prefer": "count=exact"},
            timeout=_DB_TIMEOUT,
        )
        r.raise_for_status()
        # PostgREST returns the count via Content-Range header when
        # Prefer: count=exact is set: "0-N/total"
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            return int(cr.split("/")[-1])
        return len(r.json() or [])
    except Exception:  # noqa: BLE001
        return daily_cap()


def remaining_today() -> int:
    return max(0, daily_cap() - attempts_in_last_24h())


def log_attempt(
    *,
    lesson_id: int | None,
    role: str | None,
    task: str,
    status: str,
    files_changed: int | None = None,
    lines_changed: int | None = None,
    branch: str | None = None,
    notes: str | None = None,
) -> int | None:
    if zoro_episodes._pg_url():
        try:
            with zoro_episodes._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO zoro.zoro_self_mod_log
                        (lesson_id, role, task, status, files_changed, lines_changed, branch, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id;
                        """,
                        (
                            lesson_id,
                            role,
                            task[:500],
                            status,
                            files_changed,
                            lines_changed,
                            branch,
                            (notes or "")[:500] or None,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        val = row["id"] if isinstance(row, dict) else row[0]
                        return int(val)
        except Exception:  # noqa: BLE001
            pass

    base = zoro_episodes._base()
    if base is None:
        return None
    url, headers = base
    payload: dict[str, Any] = {
        "lesson_id": lesson_id,
        "role": role,
        "task": task[:500],
        "status": status,
        "files_changed": files_changed,
        "lines_changed": lines_changed,
        "branch": branch,
        "notes": (notes or "")[:500] or None,
    }
    try:
        r = httpx.post(
            f"{url}/rest/v1/zoro_self_mod_log?select=id",
            headers={**headers, "Prefer": "return=representation"},
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


def recent_attempts(hours: int = 24) -> list[dict]:
    """Self-mod attempts in the last N hours, newest first. Fed into the
    next morning report so the user can see what Zoro tried."""
    if zoro_episodes._pg_url():
        try:
            with zoro_episodes._pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT created_at, role, task, status, files_changed,
                               lines_changed, notes
                        FROM zoro.zoro_self_mod_log
                        WHERE created_at >= NOW() - (%s || ' hours')::interval
                        ORDER BY created_at DESC
                        LIMIT 20;
                        """,
                        (str(int(hours)),),
                    )
                    rows = cur.fetchall()
                    out = []
                    for r in rows:
                        d = dict(r)
                        if isinstance(d.get("created_at"), datetime):
                            d["created_at"] = d["created_at"].isoformat()
                        out.append(d)
                    return out
        except Exception:  # noqa: BLE001
            pass

    base = zoro_episodes._base()
    if base is None:
        return []
    url, headers = base
    cutoff = quote(
        (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat()
    )
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_self_mod_log"
            f"?select=created_at,role,task,status,files_changed,"
            f"lines_changed,notes"
            f"&created_at=gte.{cutoff}"
            f"&order=created_at.desc&limit=20",
            headers=headers,
            timeout=_DB_TIMEOUT,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception:  # noqa: BLE001
        return []
