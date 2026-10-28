"""Zoro's long-term memory — Azure PostgreSQL (baseform-pg-za) with Supabase fallback.

Two tables (under schema `zoro` in PostgreSQL):
  zoro_profile  : single row (id=1) — stable, long-term facts about the user,
                  injected into the system prompt every session.
  zoro_memories : append-only facts Zoro learns over time, recalled by
                  recency or keyword. Shared across all devices.

Uses psycopg against Azure Database for PostgreSQL Flexible Server.
Synchronous; call from the agent via asyncio.to_thread so the voice loop never blocks.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_TIMEOUT = 12


def _pg_url() -> str | None:
    return os.environ.get("AZURE_POSTGRES_URL") or os.environ.get("DATABASE_URL")


def _pg_conn():
    import psycopg
    return psycopg.connect(_pg_url(), connect_timeout=_TIMEOUT, autocommit=True)


def _supabase_base() -> tuple[str, dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    return url, headers


# --- Profile (long-term) ---------------------------------------------------


def get_profile() -> dict:
    """Return {'name': str|None, 'about': str|None} for the single profile row."""
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT name, about FROM zoro.zoro_profile WHERE id = 1;")
                    row = cur.fetchone()
                    return {"name": row[0], "about": row[1]} if row else {"name": None, "about": None}
        except Exception as e:
            # Fallback to Supabase if PostgreSQL connection fails
            if "SUPABASE_URL" not in os.environ:
                return {"name": None, "about": None}

    url, h = _supabase_base()
    r = httpx.get(
        f"{url}/rest/v1/zoro_profile?id=eq.1&select=name,about",
        headers=h,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else {"name": None, "about": None}


def set_profile(name: str, about: str) -> None:
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO zoro.zoro_profile (id, name, about, updated_at)
                        VALUES (1, %s, %s, NOW())
                        ON CONFLICT (id) DO UPDATE
                        SET name = EXCLUDED.name, about = EXCLUDED.about, updated_at = NOW();
                    """, (name, about))
            return
        except Exception:
            if "SUPABASE_URL" not in os.environ:
                raise

    url, h = _supabase_base()
    r = httpx.patch(
        f"{url}/rest/v1/zoro_profile?id=eq.1",
        headers={**h, "Prefer": "return=minimal"},
        json={"name": name, "about": about},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


# --- Memories (evolving / short-term) -------------------------------------


def add_memory(content: str) -> None:
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO zoro.zoro_memories (content) VALUES (%s);", (content.strip(),))
            return
        except Exception:
            if "SUPABASE_URL" not in os.environ:
                raise

    url, h = _supabase_base()
    r = httpx.post(
        f"{url}/rest/v1/zoro_memories",
        headers={**h, "Prefer": "return=minimal"},
        json={"content": content.strip()},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def recent_memories(limit: int = 8) -> list[str]:
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT content FROM zoro.zoro_memories ORDER BY created_at DESC LIMIT %s;", (int(limit),))
                    return [row[0] for row in cur.fetchall()]
        except Exception:
            if "SUPABASE_URL" not in os.environ:
                return []

    url, h = _supabase_base()
    r = httpx.get(
        f"{url}/rest/v1/zoro_memories"
        f"?select=content&order=created_at.desc&limit={int(limit)}",
        headers=h,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return [row["content"] for row in r.json()]


def search_memories(query: str, limit: int = 6) -> list[str]:
    if _pg_url():
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    words = [w for w in query.strip().split() if w]
                    if not words:
                        return []
                    clauses = ["content ILIKE %s" for _ in words]
                    params = [f"%{w}%" for w in words] + [int(limit)]
                    cur.execute(
                        f"SELECT content FROM zoro.zoro_memories WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT %s;",
                        tuple(params),
                    )
                    return [row[0] for row in cur.fetchall()]
        except Exception:
            if "SUPABASE_URL" not in os.environ:
                return []

    url, h = _supabase_base()
    q = query.strip().replace(",", " ").replace("(", " ").replace(")", " ")
    r = httpx.get(
        f"{url}/rest/v1/zoro_memories"
        f"?select=content&content=ilike.*{q}*"
        f"&order=created_at.desc&limit={int(limit)}",
        headers=h,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return [row["content"] for row in r.json()]
