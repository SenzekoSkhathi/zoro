"""WhatsApp access via the local Node bridge (services/whatsapp).

This module is just an HTTP client. The bridge is the thing that
actually talks to WhatsApp Web — it's spawned by zoro_supervisor.
First-run authorisation is a QR scan you do once in a real terminal;
the cached session lives at data/whatsapp-session/.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

PORT = int(os.environ.get("ZORO_WHATSAPP_PORT", "8767"))
BASE = f"http://127.0.0.1:{PORT}"
TIMEOUT = httpx.Timeout(15.0)


class WhatsAppNotReady(RuntimeError):
    """Bridge is up but not yet authenticated / ready."""


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=TIMEOUT)


def status() -> dict:
    """Return {status, hasQR}. Status is one of:
    starting / qr / authenticated / ready / auth_failure / disconnected.
    """
    with _client() as c:
        r = c.get("/status")
        r.raise_for_status()
        return r.json()


def unread_chats(limit: int = 10) -> list[dict]:
    """Recent unread chats. Returns list of dicts with id, name,
    is_group, unread_count, last_body, last_ts (ms epoch)."""
    with _client() as c:
        r = c.get("/unread", params={"limit": limit})
        if r.status_code == 503:
            raise WhatsAppNotReady(r.json().get("error", "not ready"))
        r.raise_for_status()
        return r.json().get("chats", [])


def last_incoming() -> Optional[dict]:
    """Most recent message someone sent to the user (since the bridge
    last started). None if there hasn't been one."""
    with _client() as c:
        r = c.get("/last_incoming")
        r.raise_for_status()
        return r.json().get("message")


def send_message(to: str, text: str) -> dict:
    """Send a WhatsApp message.

    `to` may be a chat id (e.g. "27821234567@c.us") OR a name that
    fuzzy-matches an existing chat (case-insensitive substring).

    Returns {ok, message_id, chat_id}. Raises LookupError if the
    name doesn't match any chat, WhatsAppNotReady if the bridge
    isn't authenticated, httpx.HTTPStatusError for everything else.
    """
    with _client() as c:
        r = c.post("/send", json={"to": to, "text": text})
        if r.status_code == 503:
            raise WhatsAppNotReady(r.json().get("error", "not ready"))
        if r.status_code == 404:
            raise LookupError(r.json().get("error", "no chat match"))
        r.raise_for_status()
        return r.json()


def reply_to_last(text: str) -> dict:
    """Reply to whoever last messaged. Raises LookupError if there's
    no recent incoming message to reply to."""
    msg = last_incoming()
    if not msg:
        raise LookupError("no recent incoming WhatsApp to reply to")
    return send_message(msg["from"], text)
