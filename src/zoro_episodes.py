"""Episodic outcome log — Phase 1 telemetry foundation.

Every meaningful tool call and every computer-use run writes one row to
the `zoro_episodes` Supabase table. The voice loop never blocks on a write
(callers should fire-and-forget via asyncio.to_thread or create_task).

Read helpers:
  similar_episodes(task)  - "what happened last time I tried something like this"
                            (injected into the prompt before risky calls)
  tool_stats(days)        - per-tool success / error counts for the flakiness report
  recent_failures(limit)  - last N error rows, newest first

Schema lives in scripts/zoro_episodes.sql.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

_TIMEOUT = 8

_ERROR_PHRASE_RE = re.compile(
    r"^(i couldn't|i could not|i can't|i cannot|"
    r"my .+ failed|that .+ failed|.+ failed:|"
    r"no google account|i don't know|i hit my step limit|"
    r"action must be|search failed)",
    re.IGNORECASE,
)


def looks_like_failure(result: str | None) -> bool:
    """Heuristic: a tool returned normally but its spoken reply is a refusal
    or apology. Treat as outcome='error' so flakiness reports don't only
    count raised exceptions."""
    if not result:
        return False
    return bool(_ERROR_PHRASE_RE.match(result.strip()))


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


def _pg_url() -> str | None:
    return os.environ.get("AZURE_POSTGRES_URL") or os.environ.get("DATABASE_URL")


def _pg_conn():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(_pg_url(), connect_timeout=_TIMEOUT, autocommit=True, row_factory=dict_row)


def _trim(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def log_episode(
    *,
    kind: str,
    tool: str,
    task: str | None = None,
    args: dict[str, Any] | None = None,
    outcome: str,
    latency_ms: int | None = None,
    steps: int | None = None,
    result_summary: str | None = None,
    error: str | None = None,
    session_id: str | None = None,
) -> int | None:
    """Insert one episode row and return the new id (or None on failure).
    Best-effort: any failure is swallowed so a flaky network never breaks
    the voice loop. Call from a worker thread."""
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    safe_args = _safe_json(args)
                    cur.execute(
                        """
                        INSERT INTO zoro.zoro_episodes
                        (kind, tool, task, args, outcome, latency_ms, steps, result_summary, error, session_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id;
                        """,
                        (
                            kind,
                            tool,
                            _trim(task, 400),
                            json.dumps(safe_args) if safe_args else None,
                            outcome,
                            latency_ms,
                            steps,
                            _trim(result_summary, 400),
                            _trim(error, 400),
                            session_id,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        return int(row["id"])
        except Exception:  # noqa: BLE001 - telemetry never raises
            pass

    base = _base()
    if base is None:
        return None
    url, headers = base
    payload = {
        "kind": kind,
        "tool": tool,
        "task": _trim(task, 400),
        "args": _safe_json(args),
        "outcome": outcome,
        "latency_ms": latency_ms,
        "steps": steps,
        "result_summary": _trim(result_summary, 400),
        "error": _trim(error, 400),
        "session_id": session_id,
    }
    try:
        r = httpx.post(
            f"{url}/rest/v1/zoro_episodes?select=id",
            headers={**headers, "Prefer": "return=representation"},
            json=payload,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows, list):
            return int(rows[0]["id"])
    except Exception:  # noqa: BLE001 - telemetry never raises
        pass
    return None


def mark_corrected(episode_id: int, correction_text: str) -> bool:
    """Re-classify a previously logged episode as outcome='corrected' and
    attach the user's correction wording. Best-effort."""
    if not episode_id:
        return False
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE zoro.zoro_episodes
                        SET outcome = 'corrected', correction_text = %s
                        WHERE id = %s;
                        """,
                        (_trim(correction_text, 400), int(episode_id)),
                    )
                    return True
        except Exception:  # noqa: BLE001
            pass

    base = _base()
    if base is None or not episode_id:
        return False
    url, headers = base
    try:
        r = httpx.patch(
            f"{url}/rest/v1/zoro_episodes?id=eq.{int(episode_id)}",
            headers={**headers, "Prefer": "return=minimal"},
            json={
                "outcome": "corrected",
                "correction_text": _trim(correction_text, 400),
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe_json(args: dict[str, Any] | None) -> dict | None:
    """Make sure args round-trips through JSON; drop anything that doesn't."""
    if not args:
        return None
    out: dict[str, Any] = {}
    for k, v in args.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = repr(v)[:120]
    return out


# --- Read side -------------------------------------------------------------


def similar_episodes(task: str, limit: int = 4) -> list[dict]:
    """Return recent episodes whose task text matches `task` (ILIKE token
    match on the first two non-trivial words). Used to prime the prompt
    with 'last time you tried this' context."""
    if not task:
        return []
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", task.lower()) if len(t) > 3][:2]
    if not tokens:
        return []

    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    clauses = ["task ILIKE %s" for _ in tokens]
                    params = [f"%{t}%" for t in tokens] + [int(limit)]
                    query = f"""
                        SELECT tool, task, outcome, result_summary, error, created_at
                        FROM zoro.zoro_episodes
                        WHERE {' OR '.join(clauses)}
                        ORDER BY created_at DESC
                        LIMIT %s;
                    """
                    cur.execute(query, params)
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

    base = _base()
    if base is None:
        return []
    url, headers = base
    # OR over ILIKE on each token, newest first.
    or_clause = ",".join(f"task.ilike.*{t}*" for t in tokens)
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_episodes"
            f"?select=tool,task,outcome,result_summary,error,created_at"
            f"&or=({or_clause})"
            f"&order=created_at.desc&limit={int(limit)}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return []


def tool_stats(days: int = 7) -> list[dict]:
    """Per-tool counts of success vs error in the last `days`. Returned
    sorted by error rate descending (flakiest first)."""
    rows = []
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT tool, outcome
                        FROM zoro.zoro_episodes
                        WHERE created_at >= NOW() - (%s || ' days')::interval
                        LIMIT 5000;
                        """,
                        (str(int(days)),),
                    )
                    rows = [dict(r) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            rows = []

    if not rows:
        base = _base()
        if base is not None:
            url, headers = base
            cutoff = quote(
                (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
            )
            try:
                r = httpx.get(
                    f"{url}/rest/v1/zoro_episodes"
                    f"?select=tool,outcome"
                    f"&created_at=gte.{cutoff}"
                    f"&limit=5000",
                    headers=headers,
                    timeout=_TIMEOUT,
                )
                r.raise_for_status()
                rows = r.json()
            except Exception:  # noqa: BLE001
                rows = []

    agg: dict[str, dict[str, int]] = {}
    for row in rows:
        t = row.get("tool") or "unknown"
        a = agg.setdefault(t, {"success": 0, "error": 0, "aborted": 0, "other": 0})
        oc = row.get("outcome")
        a[oc if oc in a else "other"] += 1
    out = []
    for tool, a in agg.items():
        total = sum(a.values())
        errs = a["error"] + a["aborted"]
        out.append({
            "tool": tool,
            "total": total,
            "success": a["success"],
            "error": a["error"],
            "aborted": a["aborted"],
            "error_rate": errs / total if total else 0.0,
        })
    out.sort(key=lambda r: (-r["error_rate"], -r["total"]))
    return out


def recent_failures(limit: int = 10) -> list[dict]:
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT created_at, tool, task, outcome, error, result_summary
                        FROM zoro.zoro_episodes
                        WHERE outcome IN ('error', 'aborted')
                        ORDER BY created_at DESC
                        LIMIT %s;
                        """,
                        (int(limit),),
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

    base = _base()
    if base is None:
        return []
    url, headers = base
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_episodes"
            f"?select=created_at,tool,task,outcome,error,result_summary"
            f"&outcome=in.(error,aborted)"
            f"&order=created_at.desc&limit={int(limit)}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return []
