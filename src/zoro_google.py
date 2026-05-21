"""Read-only Google (Calendar + Gmail) access using the token that
workspace-mcp already cached during the one-time browser sign-in.

We deliberately reuse that single cached credential file rather than running
a second OAuth client, so the user only ever authorises once. Tokens are
refreshed in place and written back in the same shape workspace-mcp uses, so
the agent's MCP server and this service stay in sync.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_CRED_DIR = Path(os.path.expanduser("~")) / ".google_workspace_mcp" / "credentials"


def _cred_path(account: str) -> Path:
    return _CRED_DIR / f"{account}.json"


def _load_credentials(account: str) -> Credentials:
    raw = json.loads(_cred_path(account).read_text(encoding="utf-8"))
    creds = Credentials(
        token=raw.get("token"),
        refresh_token=raw.get("refresh_token"),
        token_uri=raw.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=raw.get("client_id"),
        client_secret=raw.get("client_secret"),
        scopes=raw.get("scopes"),
    )
    if not creds.valid:
        creds.refresh(Request())
        raw["token"] = creds.token
        if creds.expiry:
            raw["expiry"] = creds.expiry.replace(
                tzinfo=timezone.utc
            ).isoformat()
        try:
            _cred_path(account).write_text(
                json.dumps(raw, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # refresh still works in-memory this run
    return creds


def calendar_service(account: str):
    return build(
        "calendar", "v3", credentials=_load_credentials(account),
        cache_discovery=False,
    )


def gmail_service(account: str):
    return build(
        "gmail", "v1", credentials=_load_credentials(account),
        cache_discovery=False,
    )


def upcoming_events(account: str, within_minutes: int = 15) -> list[dict]:
    """Timed events starting between now and now+within_minutes.

    Returns dicts: {id, summary, start (datetime, tz-aware), minutes_until}.
    All-day events (no dateTime) are skipped — they aren't 'in N minutes'.
    """
    svc = calendar_service(account)
    now = datetime.now(timezone.utc)
    time_max = now.timestamp() + within_minutes * 60
    resp = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=datetime.fromtimestamp(
                time_max, tz=timezone.utc
            ).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=10,
        )
        .execute()
    )
    out: list[dict] = []
    for ev in resp.get("items", []):
        start = ev.get("start", {})
        dt_str = start.get("dateTime")
        if not dt_str:
            continue
        start_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        minutes_until = (start_dt - now).total_seconds() / 60.0
        out.append(
            {
                "id": ev.get("id"),
                "summary": ev.get("summary", "an event"),
                "start": start_dt,
                "minutes_until": minutes_until,
            }
        )
    return out


def agenda(account: str, hours: int = 24) -> list[dict]:
    """Upcoming events from now through now+hours, for a spoken agenda.

    Returns {summary, start, all_day} oldest-first. `start` is always a
    datetime (all-day events parse to midnight of that date) so callers
    never have to branch on its type — only on `all_day` for formatting.
    """
    svc = calendar_service(account)
    now = datetime.now(timezone.utc)
    time_max = datetime.fromtimestamp(
        now.timestamp() + hours * 3600, tz=timezone.utc
    )
    resp = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        )
        .execute()
    )
    out: list[dict] = []
    for ev in resp.get("items", []):
        start = ev.get("start", {})
        dt_str = start.get("dateTime")
        if dt_str:
            sdt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            out.append(
                {"summary": ev.get("summary", "an event"),
                 "start": sdt, "all_day": False}
            )
        elif start.get("date"):
            out.append(
                {"summary": ev.get("summary", "an event"),
                 "start": datetime.fromisoformat(start["date"]),
                 "all_day": True}
            )
    return out


def send_email(account: str, to: str, subject: str, body: str) -> None:
    """Send a plain-text email as the authenticated user."""
    import base64
    from email.mime.text import MIMEText

    svc = gmail_service(account)
    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def unread_messages(account: str, max_results: int = 8) -> list[dict]:
    """Recent unread INBOX messages: {id, from, subject, snippet}."""
    svc = gmail_service(account)
    listing = (
        svc.users()
        .messages()
        .list(userId="me", q="is:unread in:inbox", maxResults=max_results)
        .execute()
    )
    msgs: list[dict] = []
    for m in listing.get("messages", []):
        full = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )
            .execute()
        )
        headers = {
            h["name"].lower(): h["value"]
            for h in full.get("payload", {}).get("headers", [])
        }
        msgs.append(
            {
                "id": m["id"],
                "from": headers.get("from", ""),
                "subject": headers.get("subject", "(no subject)"),
                "snippet": full.get("snippet", ""),
            }
        )
    return msgs
