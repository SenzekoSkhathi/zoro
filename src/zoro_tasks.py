"""Durable task queue — 'do this thing later'.

Distinct from `zoro_tools.set_reminder`, which only speaks a string at time
T. A task carries enough context that when it fires, the proactive loop
hands Zoro a directive telling her to *carry it out* with her current tool
surface — not just announce it.

Primary backend: Azure Database for PostgreSQL (`zoro.zoro_tasks`).
Fallback: Supabase REST API (if configured and reachable).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

_TIMEOUT = 10


def _pg_url() -> str | None:
    return os.environ.get("AZURE_POSTGRES_URL") or os.environ.get("DATABASE_URL")


def _pg_conn() -> Any:
    import psycopg
    from psycopg.rows import dict_row

    url = _pg_url()
    if not url:
        raise ValueError("AZURE_POSTGRES_URL is not set")
    return psycopg.connect(
        url, connect_timeout=_TIMEOUT, autocommit=True, row_factory=dict_row  # pyright: ignore[reportArgumentType]
    )


def _supabase_base() -> tuple[str, dict] | None:
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


def _serialize_task(row: Any) -> dict[str, Any]:
    """Ensure datetime fields are ISO strings for compatibility with agents."""
    d = dict(row)
    for k in ("due_at", "created_at", "fired_at", "done_at"):
        val = d.get(k)
        if isinstance(val, datetime):
            d[k] = val.isoformat()
    return d


def add_task(due_at: datetime, title: str, detail: str | None = None) -> int | None:
    """Queue a task. Returns the new row id, or None on failure."""
    # 1. Primary: Azure PostgreSQL
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO zoro.zoro_tasks (due_at, title, detail, status)
                        VALUES (%s, %s, %s, 'pending')
                        RETURNING id;
                        """,
                        (due_at, title.strip()[:300], (detail or "").strip()[:2000] or None),
                    )
                    row = cur.fetchone()
                    if row:
                        return int(row["id"])
        except Exception as e:
            print(f"[zoro_tasks] pg add_task error: {e}", flush=True)

    # 2. Fallback: Supabase
    base = _supabase_base()
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
    # 1. Primary: Azure PostgreSQL
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, due_at, status, title, detail, created_at
                        FROM zoro.zoro_tasks
                        WHERE status = %s
                        ORDER BY due_at ASC
                        LIMIT %s;
                        """,
                        (status, int(limit)),
                    )
                    return [_serialize_task(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[zoro_tasks] pg list_tasks error: {e}", flush=True)

    # 2. Fallback: Supabase
    base = _supabase_base()
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
    cutoff = now or datetime.now(timezone.utc)
    # 1. Primary: Azure PostgreSQL
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, due_at, title, detail
                        FROM zoro.zoro_tasks
                        WHERE status = 'pending' AND due_at <= %s
                        ORDER BY due_at ASC
                        LIMIT 10;
                        """,
                        (cutoff,),
                    )
                    return [_serialize_task(r) for r in cur.fetchall()]
        except Exception as e:
            print(f"[zoro_tasks] pg due_tasks error: {e}", flush=True)

    # 2. Fallback: Supabase
    base = _supabase_base()
    if base is None:
        return []
    url, headers = base
    cutoff_iso = _utc_iso(cutoff)
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_tasks"
            f"?select=id,due_at,title,detail"
            f"&status=eq.pending&due_at=lte.{cutoff_iso}"
            f"&order=due_at.asc&limit=10",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return []


def _set_status(task_id: int, status: str, stamp_column: str | None = None) -> bool:
    # 1. Primary: Azure PostgreSQL
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    if stamp_column == "fired_at":
                        cur.execute(
                            "UPDATE zoro.zoro_tasks SET status = %s, fired_at = NOW() WHERE id = %s;",
                            (status, int(task_id)),
                        )
                    elif stamp_column == "done_at":
                        cur.execute(
                            "UPDATE zoro.zoro_tasks SET status = %s, done_at = NOW() WHERE id = %s;",
                            (status, int(task_id)),
                        )
                    else:
                        cur.execute(
                            "UPDATE zoro.zoro_tasks SET status = %s WHERE id = %s;",
                            (status, int(task_id)),
                        )
                    return cur.rowcount > 0
        except Exception as e:
            print(f"[zoro_tasks] pg _set_status error: {e}", flush=True)

    # 2. Fallback: Supabase
    base = _supabase_base()
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
    q_raw = query.strip()
    if not q_raw:
        return None

    # 1. Primary: Azure PostgreSQL
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, due_at, title, detail, status
                        FROM zoro.zoro_tasks
                        WHERE status IN ('pending', 'fired') AND title ILIKE %s
                        ORDER BY due_at ASC
                        LIMIT 1;
                        """,
                        (f"%{q_raw}%",),
                    )
                    row = cur.fetchone()
                    if row:
                        return _serialize_task(row)
        except Exception as e:
            print(f"[zoro_tasks] pg find_by_title error: {e}", flush=True)

    # 2. Fallback: Supabase
    base = _supabase_base()
    if base is None:
        return None
    url, headers = base
    q = q_raw.replace(",", " ").replace("(", " ").replace(")", " ")
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
