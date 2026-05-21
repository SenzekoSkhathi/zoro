"""Durable task queue — 'do this thing later'.

Distinct from `zoro_tools.set_reminder`, which only speaks a string at time
T. A task carries enough context that when it fires, the proactive loop
hands Zoro a directive telling her to *carry it out* with her current tool
surface — not just announce it.

Schema lives in scripts/zoro_tasks.sql.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

_TIMEOUT = 8


def _base() -> tuple[str, dict] | None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    return url.rstrip("/"), {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat()


def add_task(due_at: datetime, title: str, detail: str | None = None) -> int | None:
    """Queue a task. Returns the new row id, or None on failure."""
    base = _base()
    if base is None:
        return None
    url, headers = base
    payload = {
        "due_at": _utc_iso(due_at),
        "title": title.strip()[:300],
        "detail": (detail or "").strip()[:2000] or None,
        "status": "pending",
    }
    try:
        r = httpx.post(
            f"{url}/rest/v1/zoro_tasks?select=id",
            headers={**headers, "Prefer": "return=representation"},
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        if rows:
            return int(rows[0]["id"])
    except Exception:  # noqa: BLE001
        pass
    return None


def list_tasks(status: str = "pending", limit: int = 25) -> list[dict]:
    base = _base()
    if base is None:
        return []
    url, headers = base
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_tasks"
            f"?select=id,due_at,status,title,detail,created_at"
            f"&status=eq.{status}"
            f"&order=due_at.asc&limit={int(limit)}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return []


def due_tasks(now: datetime | None = None) -> list[dict]:
    """Pending tasks whose due_at has passed."""
    base = _base()
    if base is None:
        return []
    url, headers = base
    cutoff = _utc_iso(now or datetime.now(timezone.utc))
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_tasks"
            f"?select=id,due_at,title,detail"
            f"&status=eq.pending&due_at=lte.{cutoff}"
            f"&order=due_at.asc&limit=10",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return []


def _set_status(task_id: int, status: str, stamp_column: str | None = None) -> bool:
    base = _base()
    if base is None:
        return False
    url, headers = base
    payload: dict = {"status": status}
    if stamp_column:
        payload[stamp_column] = datetime.now(timezone.utc).isoformat()
    try:
        r = httpx.patch(
            f"{url}/rest/v1/zoro_tasks?id=eq.{int(task_id)}",
            headers={**headers, "Prefer": "return=minimal"},
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False


def mark_fired(task_id: int) -> bool:
    return _set_status(task_id, "fired", "fired_at")


def mark_done(task_id: int) -> bool:
    return _set_status(task_id, "done", "done_at")


def cancel(task_id: int) -> bool:
    return _set_status(task_id, "cancelled")


def find_by_title(query: str) -> dict | None:
    """Loose ILIKE lookup among pending tasks — for the agent's
    `cancel_task` / `mark_task_done` tools, which take a spoken phrase."""
    base = _base()
    if base is None or not query.strip():
        return None
    url, headers = base
    q = query.strip().replace(",", " ").replace("(", " ").replace(")", " ")
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_tasks"
            f"?select=id,due_at,title,detail,status"
            f"&status=in.(pending,fired)&title=ilike.*{q}*"
            f"&order=due_at.asc&limit=1",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception:  # noqa: BLE001
        return None
