"""Spotify Web API access using a cached OAuth token.

Mirror of zoro_google.py: do the one-time browser sign-in via
scripts/zoro_spotify_auth.py, then this module re-reads the cached token
at ~/.zoro_spotify/token.json and refreshes it transparently.

Playback control (play/pause/skip/queue/volume) requires Spotify Premium —
the API returns 403 on free accounts. Callers should expect SpotifyException
and surface a clear message. Playback also needs an active Spotify device
(desktop, phone, or web player) running somewhere on the account.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

_CACHE_DIR = Path(os.path.expanduser("~")) / ".zoro_spotify"
_CACHE_PATH = _CACHE_DIR / "token.json"

SCOPES = " ".join(
    [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "user-read-private",
        "user-library-read",
        "user-library-modify",
        "playlist-read-private",
        "playlist-read-collaborative",
    ]
)


def _oauth() -> SpotifyOAuth:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ.get(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
        ),
        scope=SCOPES,
        cache_path=str(_CACHE_PATH),
        open_browser=True,
    )


def _client() -> spotipy.Spotify:
    if not _CACHE_PATH.exists():
        # Fallback to environment variable (e.g. on Azure Container Apps)
        env_b64 = os.environ.get("SPOTIFY_TOKEN_B64")
        env_raw = os.environ.get("SPOTIFY_TOKEN_JSON")
        raw_text = None
        if env_b64 and env_b64 not in ("none", "disconnected", ""):
            import base64
            try:
                raw_text = base64.b64decode(env_b64).decode("utf-8")
            except Exception:
                pass
        elif env_raw and env_raw not in ("none", "disconnected", ""):
            raw_text = env_raw

        if raw_text:
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                _CACHE_PATH.write_text(raw_text.strip(), encoding="utf-8")
            except OSError:
                pass

    if not _CACHE_PATH.exists():
        raise RuntimeError(
            "Spotify is not authorised. Run "
            "`.venv/Scripts/python.exe scripts/zoro_spotify_auth.py` once."
        )
    return spotipy.Spotify(auth_manager=_oauth())


def _active_device_id(sp: spotipy.Spotify) -> Optional[str]:
    devices = sp.devices().get("devices", [])
    for d in devices:
        if d.get("is_active"):
            return d["id"]
    return devices[0]["id"] if devices else None


def now_playing() -> Optional[dict]:
    sp = _client()
    cur = sp.current_playback()
    if not cur or not cur.get("item"):
        return None
    item = cur["item"]
    album = item.get("album", {})
    # Pick the largest album-art image; Spotify returns sorted big→small.
    images = album.get("images") or []
    art_url = images[0]["url"] if images else ""
    return {
        "track": item["name"],
        "artists": ", ".join(a["name"] for a in item.get("artists", [])),
        "album": album.get("name", ""),
        "art_url": art_url,
        "is_playing": cur.get("is_playing", False),
        "progress_ms": cur.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "device": cur.get("device", {}).get("name", ""),
        "track_url": (item.get("external_urls") or {}).get("spotify", ""),
    }


def play() -> None:
    sp = _client()
    sp.start_playback(device_id=_active_device_id(sp))


def pause() -> None:
    sp = _client()
    sp.pause_playback(device_id=_active_device_id(sp))


def next_track() -> None:
    sp = _client()
    sp.next_track(device_id=_active_device_id(sp))


def previous_track() -> None:
    sp = _client()
    sp.previous_track(device_id=_active_device_id(sp))


def set_volume(percent: int) -> None:
    sp = _client()
    percent = max(0, min(100, int(percent)))
    sp.volume(percent, device_id=_active_device_id(sp))


def search_and_play(query: str, kind: str = "track") -> dict:
    """Search Spotify and start playback of the first result.

    kind: 'track' | 'album' | 'artist' | 'playlist'
    """
    if kind not in ("track", "album", "artist", "playlist"):
        raise ValueError(f"unsupported kind: {kind}")
    sp = _client()
    res = sp.search(q=query, type=kind, limit=1)
    items = res.get(f"{kind}s", {}).get("items", [])
    if not items:
        raise LookupError(f"no {kind} matching {query!r}")
    item = items[0]
    device_id = _active_device_id(sp)
    if kind == "track":
        sp.start_playback(device_id=device_id, uris=[item["uri"]])
    else:
        sp.start_playback(device_id=device_id, context_uri=item["uri"])
    artists = (
        ", ".join(a["name"] for a in item.get("artists", []))
        if kind in ("track", "album")
        else ""
    )
    result = {"name": item["name"], "artists": artists, "status": "pending"}
    # The start endpoint acknowledges a request, not actual playback. Spotify's
    # playback state can lag behind it, so check a few times before reporting.
    for attempt in range(3):
        if attempt:
            time.sleep(0.7)
        try:
            cur = sp.current_playback()
        except spotipy.SpotifyException:
            raise
        except Exception as exc:
            result["detail"] = f"Could not verify Spotify playback: {exc}"
            return result
        if not cur or not cur.get("is_playing") or not cur.get("item"):
            continue
        playing = cur["item"]
        if kind == "track":
            matches = (
                (item.get("id") and playing.get("id") == item["id"])
                or (item.get("uri") and playing.get("uri") == item["uri"])
            )
        else:
            context = cur.get("context") or {}
            matches = context.get("uri") == item["uri"]
        if matches:
            result["status"] = "playing"
            return result
        result["status"] = "wrong_track"
        result["detail"] = playing.get("name", "another track")
    if result["status"] == "pending":
        result["detail"] = "No matching active playback was confirmed."
    return result


def add_current_to_liked() -> Optional[str]:
    sp = _client()
    cur = sp.current_playback()
    if not cur or not cur.get("item"):
        return None
    sp.current_user_saved_tracks_add([cur["item"]["id"]])
    return cur["item"]["name"]


def play_playlist(name: str) -> dict:
    sp = _client()
    results = sp.current_user_playlists(limit=50)
    needle = name.lower()
    target = next(
        (p for p in results.get("items", []) if needle in p["name"].lower()),
        None,
    )
    if not target:
        raise LookupError(f"no playlist matching {name!r}")
    sp.start_playback(
        device_id=_active_device_id(sp), context_uri=target["uri"]
    )
    return {"name": target["name"]}


def list_devices() -> list[dict]:
    sp = _client()
    return [
        {"name": d["name"], "type": d["type"], "is_active": d["is_active"]}
        for d in sp.devices().get("devices", [])
    ]
