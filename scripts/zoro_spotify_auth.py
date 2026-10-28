"""One-time Spotify authorisation. Run once, then forget.

Opens a browser, you click Allow, and the token gets cached at
~/.zoro_spotify/token.json. The agent's zoro_spotify module reads
that file from then on and refreshes it transparently.

Usage:
    .venv/Scripts/python.exe scripts/zoro_spotify_auth.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import zoro_spotify  # noqa: E402


def main() -> None:
    import spotipy

    sp = spotipy.Spotify(auth_manager=zoro_spotify._oauth())
    me = sp.current_user()
    print(f"Authorised as {me['display_name']} ({me['id']}).")
    print(f"Token cached at: {zoro_spotify._CACHE_PATH}")


if __name__ == "__main__":
    main()
