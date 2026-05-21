"""Zoro's long-term memory — Supabase (PostgREST) access.

Two tables:
  zoro_profile  : single row (id=1) — stable, long-term facts about the user,
                  injected into the system prompt every session.
  zoro_memories : append-only facts Zoro learns over time, recalled by
                  recency or keyword. Shared across all devices.

Plain httpx against Supabase's REST API — no heavy SDK. Synchronous; call
from the agent via asyncio.to_thread so the voice loop never blocks.
"""

from __future__ import annotations

import os

import httpx

_TIMEOUT = 12


def _base() -> tuple[str, dict]:
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
    url, h = _base()
    r = httpx.get(
        f"{url}/rest/v1/zoro_profile?id=eq.1&select=name,about",
        headers=h,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else {"name": None, "about": None}


def set_profile(name: str, about: str) -> None:
    url, h = _base()
    r = httpx.patch(
        f"{url}/rest/v1/zoro_profile?id=eq.1",
        headers={**h, "Prefer": "return=minimal"},
        json={"name": name, "about": about},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


# --- Memories (evolving / short-term) -------------------------------------


def add_memory(content: str) -> None:
    url, h = _base()
    r = httpx.post(
        f"{url}/rest/v1/zoro_memories",
        headers={**h, "Prefer": "return=minimal"},
        json={"content": content.strip()},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()


def recent_memories(limit: int = 8) -> list[str]:
    url, h = _base()
    r = httpx.get(
        f"{url}/rest/v1/zoro_memories"
        f"?select=content&order=created_at.desc&limit={int(limit)}",
        headers=h,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return [row["content"] for row in r.json()]


def search_memories(query: str, limit: int = 6) -> list[str]:
    url, h = _base()
    # PostgREST ILIKE: wildcards as * ; commas/parens escaped to keep the
    # query-string filter well-formed.
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
