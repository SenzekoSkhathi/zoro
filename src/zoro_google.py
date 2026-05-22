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


def find_events(account: str, query: str, max_results: int = 5) -> list[dict]:
    """Full-text search upcoming events by `query`. Returns
    {id, summary, start (tz-aware datetime), all_day}, soonest first."""
    svc = calendar_service(account)
    now = datetime.now(timezone.utc)
    resp = (
        svc.events()
        .list(
            calendarId="primary",
            q=query,
            timeMin=now.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    out: list[dict] = []
    for ev in resp.get("items", []):
        start = ev.get("start", {})
        dt_str = start.get("dateTime")
        if dt_str:
            sdt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            out.append({"id": ev["id"], "summary": ev.get("summary", "an event"),
                        "start": sdt, "all_day": False})
        elif start.get("date"):
            out.append({"id": ev["id"], "summary": ev.get("summary", "an event"),
                        "start": datetime.fromisoformat(start["date"]),
                        "all_day": True})
    return out


def create_event(
    account: str, summary: str, start_iso: str, end_iso: str,
    description: str | None = None,
) -> dict:
    """Create a timed event on the primary calendar. start_iso/end_iso are
    ISO 8601 with an offset (e.g. 2026-05-23T14:00:00+02:00). Returns
    {id, summary, htmlLink}."""
    svc = calendar_service(account)
    body: dict = {
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if description:
        body["description"] = description
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    return {"id": ev.get("id"), "summary": ev.get("summary"),
            "htmlLink": ev.get("htmlLink")}


def move_event(account: str, event_id: str, start_iso: str, end_iso: str) -> dict:
    """Reschedule an event to a new start/end. Returns {id, summary}."""
    svc = calendar_service(account)
    ev = svc.events().patch(
        calendarId="primary", eventId=event_id,
        body={"start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}},
    ).execute()
    return {"id": ev.get("id"), "summary": ev.get("summary")}


def cancel_event(account: str, event_id: str) -> None:
    """Delete an event from the primary calendar."""
    svc = calendar_service(account)
    svc.events().delete(calendarId="primary", eventId=event_id).execute()


def respond_to_event(account: str, event_id: str, accept: bool) -> dict:
    """Set the user's own attendee responseStatus on an invite. Returns
    {summary, status}. Raises if the user isn't an attendee."""
    svc = calendar_service(account)
    ev = svc.events().get(calendarId="primary", eventId=event_id).execute()
    attendees = ev.get("attendees", [])
    status = "accepted" if accept else "declined"
    changed = False
    for a in attendees:
        if a.get("self"):
            a["responseStatus"] = status
            changed = True
    if not changed:
        raise ValueError("you are not an attendee on that event")
    updated = svc.events().patch(
        calendarId="primary", eventId=event_id,
        body={"attendees": attendees}, sendUpdates="all",
    ).execute()
    return {"summary": updated.get("summary"), "status": status}


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


# --- Gmail deep ops (Phase 3 new surface) ----------------------------------


def _header_map(payload: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in payload.get("headers", [])
    }


def _extract_plain_body(payload: dict) -> str:
    """Walk a Gmail payload tree and return decoded text. Prefers
    text/plain; falls back to a crude tag-strip of text/html."""
    import base64
    import re

    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode(
            "utf-8", errors="replace"
        )

    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        text = re.sub(r"<[^>]+>", " ", "\n".join(html))
        return re.sub(r"[ \t]+", " ", text).strip()
    return ""


def search_messages(account: str, query: str, max_results: int = 8) -> list[dict]:
    """Search mail by a Gmail query (any folder). Returns
    {id, thread_id, from, subject, date, snippet}, newest first."""
    svc = gmail_service(account)
    listing = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    out: list[dict] = []
    for m in listing.get("messages", []):
        full = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        h = _header_map(full.get("payload", {}))
        out.append(
            {
                "id": m["id"],
                "thread_id": full.get("threadId"),
                "from": h.get("from", ""),
                "subject": h.get("subject", "(no subject)"),
                "date": h.get("date", ""),
                "snippet": full.get("snippet", ""),
            }
        )
    return out


def get_message(account: str, message_id: str) -> dict:
    """Full message by id: {id, thread_id, from, to, subject, date, body}."""
    svc = gmail_service(account)
    full = (
        svc.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    payload = full.get("payload", {})
    h = _header_map(payload)
    return {
        "id": message_id,
        "thread_id": full.get("threadId"),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "subject": h.get("subject", "(no subject)"),
        "date": h.get("date", ""),
        "body": _extract_plain_body(payload),
    }


def find_one_message(account: str, query: str) -> dict | None:
    """Best single match for a spoken query — newest message matching it,
    fully fetched. None if nothing matches."""
    hits = search_messages(account, query, max_results=1)
    if not hits:
        return None
    return get_message(account, hits[0]["id"])


def reply_to_message(account: str, message_id: str, body: str) -> dict:
    """Reply in-thread to a message. Reuses the original subject (with a
    Re: prefix) and sets In-Reply-To/References so Gmail threads it.
    Returns {to, subject}."""
    import base64
    from email.mime.text import MIMEText

    svc = gmail_service(account)
    orig = (
        svc.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID", "References"],
        )
        .execute()
    )
    h = _header_map(orig.get("payload", {}))
    thread_id = orig.get("threadId")
    to = h.get("from", "")
    subject = h.get("subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg_id_hdr = h.get("message-id", "")
    references = (h.get("references", "") + " " + msg_id_hdr).strip()

    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    if msg_id_hdr:
        mime["In-Reply-To"] = msg_id_hdr
    if references:
        mime["References"] = references
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    svc.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id}
    ).execute()
    return {"to": to, "subject": subject}


def modify_message(
    account: str, message_id: str,
    add: list[str] | None = None, remove: list[str] | None = None,
) -> None:
    """Add/remove Gmail label ids on a message. INBOX/UNREAD are labels:
    archive = remove INBOX; mark read = remove UNREAD."""
    svc = gmail_service(account)
    svc.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": add or [], "removeLabelIds": remove or []},
    ).execute()


def archive_message(account: str, message_id: str) -> None:
    modify_message(account, message_id, remove=["INBOX"])


def mark_read(account: str, message_id: str, read: bool = True) -> None:
    if read:
        modify_message(account, message_id, remove=["UNREAD"])
    else:
        modify_message(account, message_id, add=["UNREAD"])
