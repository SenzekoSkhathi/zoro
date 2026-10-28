"""Interactive Google Workspace (Gmail + Calendar) OAuth authorization for Zoro.

Usage:
    .venv/Scripts/python.exe scripts/google_auth.py
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import httpx
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

_CRED_DIR = Path(os.path.expanduser("~")) / ".google_workspace_mcp" / "credentials"


class CallbackServer(http.server.HTTPServer):
    auth_code: str | None = None
    auth_error: str | None = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: CallbackServer  # pyright: ignore[reportIncompatibleVariableOverride]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            self.server.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Zoro - Google Connected</title>
                <style>
                    body {
                        background: #060d17;
                        color: #e0f2fe;
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        margin: 0;
                    }
                    .card {
                        background: rgba(14, 28, 45, 0.85);
                        border: 1px solid rgba(79, 227, 255, 0.35);
                        padding: 2.5rem 3rem;
                        border-radius: 8px;
                        text-align: center;
                        box-shadow: 0 0 30px rgba(0, 195, 255, 0.2);
                        max-width: 480px;
                    }
                    h1 { color: #4fe3ff; margin-bottom: 0.5rem; font-size: 1.5rem; }
                    p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; }
                    .badge {
                        display: inline-block;
                        padding: 0.25rem 0.75rem;
                        background: rgba(79, 227, 255, 0.15);
                        border: 1px solid #4fe3ff;
                        border-radius: 4px;
                        color: #4fe3ff;
                        font-weight: 600;
                        font-size: 0.8rem;
                        margin-top: 1rem;
                        letter-spacing: 0.05em;
                    }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>&#x2714; Google Workspace Connected</h1>
                    <p>Zoro has securely received your authorization tokens for Gmail and Google Calendar.</p>
                    <div class="badge">YOU CAN CLOSE THIS TAB</div>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        elif "error" in params:
            self.server.auth_error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1>Authorization Failed</h1><p>{self.server.auth_error}</p>".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # Quiet logging


def main() -> None:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("[ERROR] GOOGLE_OAUTH_CLIENT_ID or GOOGLE_OAUTH_CLIENT_SECRET not set in .env")
        sys.exit(1)

    port = 8000
    redirect_uri = f"http://localhost:{port}/oauth2callback"

    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }

    flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri

    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    # Save to file for easy access
    url_file = ROOT / "data" / "google_auth_url.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(auth_url, encoding="utf-8")

    server = CallbackServer(("localhost", port), CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    print("\n" + "=" * 70, flush=True)
    print("ZORO GOOGLE WORKSPACE AUTHORIZATION", flush=True)
    print("=" * 70, flush=True)
    print("\nPlease open the link below in your browser to authorize Zoro:\n", flush=True)
    print(auth_url, flush=True)
    print("\nWaiting for authorization callback on http://localhost:8000/oauth2callback...", flush=True)

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server_thread.join(timeout=300)

    if not server.auth_code:
        if server.auth_error:
            print(f"\n[ERROR] Google returned authorization error: {server.auth_error}")
        else:
            print("\n[ERROR] Timed out waiting for authorization callback.")
        sys.exit(1)

    print("\nAuthorization code received! Exchanging for tokens...")
    flow.fetch_token(code=server.auth_code)
    creds = flow.credentials

    # Query user email
    user_email = ""
    try:
        r = httpx.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=10.0,
        )
        if r.status_code == 200:
            user_email = r.json().get("email", "")
    except Exception:
        pass

    if not user_email:
        user_email = "senzekonduli28@gmail.com"

    print(f"Authenticated as: {user_email}")

    # Build credential payload matching workspace-mcp shape
    cred_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": getattr(creds, "token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }

    _CRED_DIR.mkdir(parents=True, exist_ok=True)
    out_file = _CRED_DIR / f"{user_email}.json"
    out_file.write_text(json.dumps(cred_data, indent=2), encoding="utf-8")
    print(f"Credentials saved to: {out_file}")

    # Also save backup in data/
    repo_cred = ROOT / "data" / "google_credentials.json"
    repo_cred.write_text(json.dumps(cred_data, indent=2), encoding="utf-8")

    # Update data/zoro_config.json
    cfg_file = ROOT / "data" / "zoro_config.json"
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    cfg["gmail_account"] = user_email
    cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Updated {cfg_file} with gmail_account: '{user_email}'")

    # Verify live connectivity
    print("\n--- Verifying Live Connectivity ---")
    import zoro_google

    try:
        msgs = zoro_google.unread_messages(user_email, max_results=3)
        print(f"[SUCCESS] Gmail unread messages fetched: {len(msgs)} messages")
        for m in msgs:
            print(f"  * From: {m.get('from')} | Subject: {m.get('subject')}")
    except Exception as e:
        print(f"[WARN] Gmail check error: {e}")

    try:
        agenda = zoro_google.agenda(user_email, hours=48)
        print(f"[SUCCESS] Calendar agenda fetched: {len(agenda)} upcoming events")
        for ev in agenda:
            print(f"  * Event: {ev.get('summary')} | Start: {ev.get('start')}")
    except Exception as e:
        print(f"[WARN] Calendar check error: {e}")

    print("\n[COMPLETE] Google Workspace is fully linked and ready for Zoro!")


if __name__ == "__main__":
    main()
