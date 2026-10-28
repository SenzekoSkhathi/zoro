"""Reflection layer — turn failed (and notable successful) episodes into lessons.

When a tool call ends in outcome='error', the @_logged wrapper fires
reflect_on_episode() in a worker thread. That call:

  1. Loads the episode + a few similar past episodes
  2. Asks Claude Haiku (cheap, fast) for a one-line lesson and a concrete
     suggested improvement
  3. Writes a row to `zoro_lessons` (Supabase)

Read helpers feed the nightly self-improvement job and the morning report.
Schema lives in scripts/zoro_lessons.sql.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import httpx

from zoro_episodes import _base, _trim, similar_episodes, _pg_url, _pg_conn  # type: ignore

_REFLECT_MODEL = "claude-haiku-4-5"
_REFLECT_TIMEOUT = 20
_SUPABASE_TIMEOUT = 8

_REFLECT_SYSTEM = (
    "You are Zoro's reflection layer. Given one failed tool call (and a "
    "few similar past ones), output a strict JSON object with these keys:\n"
    '  "failure_summary": one short clause naming what went wrong\n'
    '  "lesson":          one sentence starting with "Next time..." that '
    "you, Zoro, would tell yourself to do differently\n"
    '  "suggested_improvement": one concrete change. Prefer naming a '
    "file in src/ and the change (e.g. \"add a retry to "
    "zoro_google.check_email when the token is stale\"). If the real "
    "blocker is missing access/tool/API key, instead write what's "
    "needed and set needs_resource to that string.\n"
    '  "needs_resource":  null, or a short label of the missing access '
    "(e.g. \"Notion API key\", \"webcam permission\")\n"
    "Be terse. No prose outside the JSON. No markdown."
)


def _supabase_headers() -> tuple[str, dict] | None:
    return _base()


def _call_azure_openai(user_msg: str) -> dict | None:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    dep = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-6-sol")
    if not endpoint or not key:
        return None
    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=key,
            api_version=os.environ.get("OPENAI_API_VERSION", "2024-10-21"),
        )
        resp = client.chat.completions.create(
            model=dep,
            messages=[
                {"role": "system", "content": _REFLECT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=400,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return None
    return None


def reflect_on_episode(
    *,
    episode_id: int | None,
    tool: str,
    task: str | None,
    outcome: str,
    result_summary: str | None,
    error: str | None,
) -> int | None:
    """Generate a lesson for one episode and write it to zoro_lessons.

    Returns the new lesson id, or None if the write was skipped (no key,
    LLM failure, DB down, etc.). Best-effort: never raises.
    """
    if outcome not in ("error",):
        # Phase 1: only learn from failures. Successful-improvement
        # reflection can come later (it's noisier and lower-value).
        return None

    # Build the reflector prompt from the episode + a few similar ones.
    try:
        prior = similar_episodes(task or tool, limit=3)
    except Exception:  # noqa: BLE001
        prior = []
    prior_blocks = "\n".join(
        f"- {p.get('created_at', '')[:10]} {p.get('tool')}: "
        f"{p.get('outcome')} — {(p.get('error') or p.get('result_summary') or '')[:140]}"
        for p in prior
    ) or "(none)"
    user_msg = (
        f"Tool: {tool}\n"
        f"Task: {task or '(none)'}\n"
        f"Outcome: {outcome}\n"
        f"Error: {error or '(none)'}\n"
        f"Result said: {result_summary or '(none)'}\n\n"
        f"Similar past episodes:\n{prior_blocks}\n"
    )

    lesson_obj = _call_azure_openai(user_msg)
    if not lesson_obj:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            lesson_obj = _call_claude(api_key, user_msg)
    if not lesson_obj:
        return None

    failure_summary = _trim(lesson_obj.get("failure_summary"), 400)
    lesson_text = _trim(lesson_obj.get("lesson"), 400) or "(empty)"
    suggested_improvement = _trim(lesson_obj.get("suggested_improvement"), 400)
    needs_resource = _trim(lesson_obj.get("needs_resource"), 200)

    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO zoro.zoro_lessons
                        (episode_id, tool, task, outcome, failure_summary, lesson, suggested_improvement, needs_resource, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id;
                        """,
                        (
                            episode_id,
                            tool,
                            _trim(task, 400),
                            outcome,
                            failure_summary,
                            lesson_text,
                            suggested_improvement,
                            needs_resource,
                            "open",
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        return int(row["id"])
        except Exception:  # noqa: BLE001
            pass

    base = _supabase_headers()
    if base is None:
        return None
    url, headers = base
    payload = {
        "episode_id": episode_id,
        "tool": tool,
        "task": _trim(task, 400),
        "outcome": outcome,
        "failure_summary": failure_summary,
        "lesson": lesson_text,
        "suggested_improvement": suggested_improvement,
        "needs_resource": needs_resource,
        "status": "open",
    }
    try:
        r = httpx.post(
            f"{url}/rest/v1/zoro_lessons?select=id",
            headers={**headers, "Prefer": "return=representation"},
            json=payload,
            timeout=_SUPABASE_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        if rows and isinstance(rows, list):
            return int(rows[0]["id"])
    except Exception:  # noqa: BLE001
        return None
    return None


def _call_claude(api_key: str, user_msg: str) -> dict | None:
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
                "max_tokens": 400,
                "system": _REFLECT_SYSTEM,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=_REFLECT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        text = text.strip()
        # Tolerate the model wrapping the JSON in code fences anyway.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        return None
    return None


# --- Read side -------------------------------------------------------------


def open_lessons(limit: int = 20) -> list[dict]:
    """Lessons not yet reported or attempted, newest first."""
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, created_at, episode_id, tool, task, outcome,
                               failure_summary, lesson, suggested_improvement,
                               needs_resource, status, attempted_at,
                               attempted_branch, attempted_result
                        FROM zoro.zoro_lessons
                        WHERE status = 'open'
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
                        if isinstance(d.get("attempted_at"), datetime):
                            d["attempted_at"] = d["attempted_at"].isoformat()
                        out.append(d)
                    return out
        except Exception:  # noqa: BLE001
            pass

    base = _supabase_headers()
    if base is None:
        return []
    url, headers = base
    try:
        r = httpx.get(
            f"{url}/rest/v1/zoro_lessons"
            f"?status=eq.open&order=created_at.desc&limit={int(limit)}",
            headers=headers,
            timeout=_SUPABASE_TIMEOUT,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception:  # noqa: BLE001
        return []


def mark_status(lesson_id: int, status: str, **fields: Any) -> bool:
    """Update a lesson row's status (and optional attempted_* fields)."""
    if not lesson_id:
        return False

    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    set_clauses = ["status = %s"]
                    params: list[Any] = [status]
                    for k in ("attempted_at", "attempted_branch", "attempted_result"):
                        if k in fields and fields[k] is not None:
                            set_clauses.append(f"{k} = %s")
                            params.append(fields[k])
                    params.append(int(lesson_id))
                    cur.execute(
                        f"""
                        UPDATE zoro.zoro_lessons
                        SET {', '.join(set_clauses)}
                        WHERE id = %s;
                        """,
                        params,
                    )
                    return True
        except Exception:  # noqa: BLE001
            pass

    base = _supabase_headers()
    if base is None:
        return False
    url, headers = base
    payload: dict[str, Any] = {"status": status}
    for k in ("attempted_at", "attempted_branch", "attempted_result"):
        if k in fields and fields[k] is not None:
            payload[k] = fields[k]
    try:
        r = httpx.patch(
            f"{url}/rest/v1/zoro_lessons?id=eq.{int(lesson_id)}",
            headers={**headers, "Prefer": "return=minimal"},
            json=payload,
            timeout=_SUPABASE_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False
