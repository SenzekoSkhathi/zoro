"""Pure helper logic for Zoro's tools (no LiveKit imports).

Kept separate from the agent so it stays testable and the agent file stays
about wiring, not implementation.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

# --- Notes -----------------------------------------------------------------

NOTES_FILE = Path(__file__).resolve().parent.parent / "data" / "notes.txt"

# --- Self source access ----------------------------------------------------

ZORO_ROOT = Path(__file__).resolve().parent.parent
SELF_SOURCE_ROOTS = ("src", "scripts")
MAX_SOURCE_CHARS = 24000


def read_self_source(name: str) -> str:
    """Return the contents of one of Zoro's own source files.

    `name` may be a bare module name (`zoro_proactive`), filename
    (`zoro_proactive.py`), or repo-relative path (`src/zoro_proactive.py`).
    Confined to `src/` and `scripts/` — anything else is refused. Output
    is truncated to keep an oversized file from blowing the LLM context.
    """
    n = name.strip().replace("\\", "/")
    if not n or ".." in n.split("/"):
        return "I can't read that path."
    candidates: list[Path] = []
    if "/" in n:
        candidates.append(ZORO_ROOT / n)
    else:
        bare = n if n.endswith(".py") else f"{n}.py"
        for root in SELF_SOURCE_ROOTS:
            candidates.append(ZORO_ROOT / root / bare)
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        # Hard-confine to allowed roots.
        if not any(
            str(resolved).startswith(str((ZORO_ROOT / r).resolve()))
            for r in SELF_SOURCE_ROOTS
        ):
            continue
        text = resolved.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_SOURCE_CHARS:
            text = (
                text[:MAX_SOURCE_CHARS]
                + f"\n\n... (truncated, {len(text)} chars total)"
            )
        rel = resolved.relative_to(ZORO_ROOT).as_posix()
        return f"# {rel}\n\n{text}"
    return f"I couldn't find a source file matching '{name}'."


def append_note(text: str) -> None:
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {text.strip()}\n")


def read_notes(limit: int = 5) -> list[str]:
    if not NOTES_FILE.exists():
        return []
    lines = [ln.strip() for ln in NOTES_FILE.read_text("utf-8").splitlines() if ln.strip()]
    return lines[-limit:]


# --- Durable reminders -----------------------------------------------------

REMINDERS_FILE = Path(__file__).resolve().parent.parent / "data" / "reminders.json"


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON via a temp file in the same dir + os.replace, so a
    concurrent reader (the proactive loop) never sees a half-written
    file. Mirrors the atomic-write pattern zoro_safety uses for its
    state files. Raises OSError if the write genuinely fails."""
    import json
    import tempfile
    import time as _time

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # On Windows os.replace raises PermissionError if another process
        # has the destination open — readers do, very briefly. The
        # contention window is sub-millisecond, so retry before failing.
        for attempt in range(50):
            try:
                os.replace(tmp, path)
                tmp = None
                return
            except PermissionError:
                if attempt == 49:
                    raise
                _time.sleep(0.02)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


_LOCK_TIMEOUT = 5.0
_LOCK_STALE = 30.0


@contextlib.contextmanager
def _reminders_lock():
    """Cross-process mutex for the reminders.json read-modify-write.

    Atomic replace stops torn reads; this stops *lost updates* when the
    agent (add_reminder) and the proactive loop (mark_reminder_fired)
    run in separate processes and interleave their read→modify→write.
    A lock orphaned by a crashed process is reclaimed after _LOCK_STALE.
    """
    lock = REMINDERS_FILE.with_name(REMINDERS_FILE.name + ".lock")
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + _LOCK_TIMEOUT
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except (FileExistsError, PermissionError):
            # FileExistsError: lock held. PermissionError: Windows returns
            # ACCESS_DENIED (not EEXIST) when the lock file is mid-unlink
            # by the previous holder ("delete pending") — also just
            # contention, so retry the same way.
            try:
                if time.time() - os.path.getmtime(lock) > _LOCK_STALE:
                    os.unlink(lock)  # previous holder crashed — reclaim
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError("reminders.json is locked")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(lock)
            except OSError:
                pass


def _read_reminders() -> list:
    """Read the reminders list.

    [] for a genuinely absent or corrupt file (the original behaviour).
    A transient PermissionError — a reader hitting the sub-millisecond
    window while another process os.replace()s the file on Windows — is
    retried, then RAISED if it never clears. We must never treat a
    transient lock as 'no reminders': that would make add_reminder
    overwrite the file and silently destroy every existing reminder.
    """
    import json
    import time as _time

    for attempt in range(50):
        try:
            data = json.loads(REMINDERS_FILE.read_text("utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except ValueError:
            return []
        except PermissionError:
            if attempt == 49:
                raise
            _time.sleep(0.02)
    return []


def read_reminders() -> list:
    """Lock-protected snapshot of the reminders list for read-only
    callers (the proactive watcher). Reading under the same lock as the
    writers means a reader never collides with an in-progress os.replace
    on Windows. Best-effort: returns [] if the lock can't be taken — a
    missed poll is harmless, it's re-checked next cycle.
    """
    try:
        with _reminders_lock():
            return _read_reminders()
    except (OSError, TimeoutError):
        return []


def add_reminder(seconds: int, text: str) -> None:
    """Persist a reminder so the proactive loop fires it independently of
    any live session. The whole read-modify-write is serialised so a
    concurrent mark-fired can never drop the new reminder."""
    import uuid

    with _reminders_lock():
        items = _read_reminders()
        items.append(
            {
                "id": uuid.uuid4().hex[:12],
                "due": time.time() + max(1, int(seconds)),
                "text": text.strip(),
                "fired": False,
            }
        )
        _atomic_write_json(REMINDERS_FILE, items)


def mark_reminder_fired(rem_id: str) -> None:
    """Durably mark one reminder consumed, under the same cross-process
    lock as add_reminder so the two never clobber each other.

    Best-effort: a transient failure or lock timeout is swallowed (the
    proactive layer's announced-TTL dedupe is the backstop) — but it can
    never silently destroy other reminders.
    """
    try:
        with _reminders_lock():
            items = _read_reminders()
            changed = False
            for r in items:
                if r.get("id") == rem_id and not r.get("fired"):
                    r["fired"] = True
                    changed = True
            if changed:
                _atomic_write_json(REMINDERS_FILE, items)
    except (OSError, TimeoutError):
        pass


# --- Web search ------------------------------------------------------------


def search_web_detailed(query: str, max_results: int = 4) -> dict:
    """Web search via Tavily's REST API (TAVILY_API_KEY), full payload.

    Plain httpx — no SDK — matching the rest of the codebase. Returns
    both Tavily's synthesised `answer` (ideal for a spoken reply) and the
    individual source results, so callers can speak the answer while the
    web view shows the sources.

    Returns: {"query", "answer", "results": [{"title","url","content"}]}.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    r = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": True,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    results = []
    for item in data.get("results") or []:
        results.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": (item.get("url") or "").strip(),
                "content": (item.get("content") or "").strip(),
            }
        )
    return {
        "query": query,
        "answer": (data.get("answer") or "").strip(),
        "results": results,
    }


def search_web(query: str, max_results: int = 4) -> str:
    """Web search returning a single string suitable for a spoken reply.

    Prefers Tavily's synthesised `answer`; falls back to the top result
    snippets if it isn't returned.
    """
    data = search_web_detailed(query, max_results)
    if data["answer"]:
        return data["answer"]
    if not data["results"]:
        return "No results found."
    return "\n".join(
        f"{r['title']}: {r['content']}" for r in data["results"]
    )


# --- Weather (open-meteo + IP geolocation, no API key) ---------------------

_WEATHER_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _geolocate_ip() -> tuple[float, float, str]:
    # ip-api.com free tier is rate-limited (45 req/min) and answers with
    # {"status":"fail","message":...} — and no lat/lon — when throttled or
    # for a private IP. Surface that as a clear RuntimeError so callers can
    # degrade gracefully, instead of an opaque KeyError on d["lat"].
    r = httpx.get(
        "http://ip-api.com/json/?fields=status,message,lat,lon,city",
        timeout=10,
    )
    d = r.json()
    if d.get("status") != "success" or "lat" not in d or "lon" not in d:
        raise RuntimeError(
            "IP geolocation unavailable: "
            + (d.get("message") or "no location in response")
        )
    return d["lat"], d["lon"], d.get("city", "your location")


def get_location() -> str:
    """Approximate location via IP — spoken summary."""
    try:
        d = get_location_detailed()
    except RuntimeError:
        return "I couldn't determine your location right now."
    return (
        f"You appear to be in {d['city']}, {d['region']}, "
        f"{d['country']}. Timezone {d['timezone']}."
    )


def _geocode(name: str) -> tuple[float, float, str]:
    r = httpx.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 1},
        timeout=10,
    )
    res = r.json().get("results")
    if not res:
        raise ValueError(f"couldn't find a place called {name}")
    p = res[0]
    label = ", ".join(x for x in (p.get("name"), p.get("country")) if x)
    return p["latitude"], p["longitude"], label


def get_weather_detailed(place: str | None = None) -> dict:
    """Structured current weather + today's range, for the web view.

    Returns {place, temp, condition, wind, high, low}. `get_weather`
    delegates here for its spoken string so the two never diverge.
    """
    if place:
        lat, lon, label = _geocode(place)
    else:
        lat, lon, label = _geolocate_ip()
    r = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=10,
    )
    d = r.json()
    cur = d["current"]
    daily = d["daily"]
    return {
        "place": label,
        "temp": round(cur["temperature_2m"]),
        "condition": _WEATHER_CODES.get(
            cur["weather_code"], "unclear conditions"
        ),
        "wind": round(cur["wind_speed_10m"]),
        "high": round(daily["temperature_2m_max"][0]),
        "low": round(daily["temperature_2m_min"][0]),
    }


def get_weather(place: str | None = None) -> str:
    w = get_weather_detailed(place)
    return (
        f"In {w['place']}: currently {w['temp']} degrees, "
        f"{w['condition']}, wind {w['wind']} kilometres per hour. "
        f"Today's high {w['high']} degrees, low {w['low']} degrees."
    )


def _fmt_hour(iso_local: str) -> str:
    """'2026-05-16T14:00' -> '2 PM'."""
    h = int(iso_local[11:13])
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {period}"


def get_morning_weather_detailed(place: str | None = None) -> dict:
    """Structured morning weather for the briefing card.

    Returns {place, temp, condition, high, low, rain} where `rain` is the
    natural heads-up sentence. `get_morning_weather` delegates here for
    its spoken string so the two never diverge.
    """
    if place:
        lat, lon, label = _geocode(place)
    else:
        lat, lon, label = _geolocate_ip()
    r = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min",
            "hourly": "precipitation_probability,weather_code",
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=10,
    )
    d = r.json()
    cur, daily, hourly = d["current"], d["daily"], d["hourly"]
    times = hourly["time"]
    probs = hourly["precipitation_probability"]
    cur_hour = cur["time"][:13]  # 'YYYY-MM-DDTHH'
    start = next(
        (i for i, t in enumerate(times) if t[:13] >= cur_hour), len(times)
    )
    rain = "No significant rain expected for the rest of the day."
    for i in range(start, len(times)):
        p = probs[i]
        if p is not None and p >= 55:
            rain = (
                f"Heads up: rain looks likely around {_fmt_hour(times[i])} "
                f"(about {p} percent chance), so keep an umbrella handy."
            )
            break
    return {
        "place": label,
        "temp": round(cur["temperature_2m"]),
        "condition": _WEATHER_CODES.get(
            cur["weather_code"], "unclear conditions"
        ),
        "high": round(daily["temperature_2m_max"][0]),
        "low": round(daily["temperature_2m_min"][0]),
        "rain": rain,
    }


def get_morning_weather(place: str | None = None) -> str:
    """Current conditions, today's high/low, and a natural rain heads-up
    derived from the hourly forecast (e.g. 'rain likely around 2 PM')."""
    w = get_morning_weather_detailed(place)
    return (
        f"In {w['place']}, it's currently {w['temp']} degrees "
        f"and {w['condition']}. Today's high is {w['high']}, "
        f"low {w['low']} degrees. {w['rain']}"
    )


def _ip_country() -> str:
    """User's ISO country code via IP, for localising news. Falls back to
    'ZA' (the previous hard-coded default) so news never regresses or
    raises when geolocation is unavailable."""
    try:
        r = httpx.get(
            "http://ip-api.com/json/?fields=status,countryCode", timeout=8
        )
        d = r.json()
        if d.get("status") == "success" and d.get("countryCode"):
            return d["countryCode"]
    except Exception:  # noqa: BLE001 - best effort, fall back to default
        pass
    return "ZA"


def get_news_detailed(limit: int = 5) -> list[dict]:
    """Top local news via Google News RSS as {title, link, source}.

    The edition follows the user's country (by IP); English, ZA fallback.
    `get_news` delegates here for its headline-only spoken list.
    """
    import xml.etree.ElementTree as ET

    cc = _ip_country()
    r = httpx.get(
        f"https://news.google.com/rss?hl=en-{cc}&gl={cc}&ceid={cc}:en",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=12,
    )
    root = ET.fromstring(r.content)
    items: list[dict] = []
    for item in root.findall(".//item")[: int(limit)]:
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Google News titles are "Headline - Publisher"; keep the headline.
        items.append(
            {
                "title": title.rsplit(" - ", 1)[0].strip(),
                "link": (item.findtext("link") or "").strip(),
                "source": (item.findtext("source") or "").strip(),
            }
        )
    return items


def get_news(limit: int = 5) -> list[str]:
    """Top local news headlines (spoken list)."""
    return [n["title"] for n in get_news_detailed(limit)]


def get_location_detailed() -> dict:
    """Approximate location via IP as {city, region, country, timezone}.

    `get_location` delegates here for its spoken sentence.
    """
    r = httpx.get(
        "http://ip-api.com/json/"
        "?fields=status,city,regionName,country,timezone",
        timeout=10,
    )
    d = r.json()
    if d.get("status") != "success":
        raise RuntimeError("IP geolocation unavailable")
    return {
        "city": d.get("city", ""),
        "region": d.get("regionName", ""),
        "country": d.get("country", ""),
        "timezone": d.get("timezone", ""),
    }


# --- Windows system control (no extra deps) --------------------------------

# Virtual-key codes for media/volume keys.
_VK = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
    "play_pause": 0xB3,
    "next_track": 0xB0,
    "prev_track": 0xB1,
}


def _press(vk: int, repeat: int = 1) -> None:
    user32 = ctypes.windll.user32
    for _ in range(repeat):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def media_key(action: str, repeat: int = 1) -> None:
    if action not in _VK:
        raise ValueError(f"unknown media action: {action}")
    _press(_VK[action], repeat)


# --- Screen vision -------------------------------------------------------


def capture_screen_b64(max_edge: int = 1568) -> tuple[str, str]:
    """Grab the primary monitor, downscale for Claude vision, return
    (base64 png, media_type). max_edge keeps the long side within Claude's
    recommended size so the call stays fast and cheap."""
    import base64
    import io

    import mss
    from PIL import Image

    with mss.mss() as sct:
        mon = sct.monitors[1]  # [0] is the virtual all-monitors box
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    w, h = img.size
    scale = min(1.0, max_edge / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/png"


def _vision_answer(
    b64: str, media_type: str, question: str, api_key: str
) -> str:
    import os

    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_dep = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-6-sol")

    if azure_endpoint and azure_key:
        try:
            from openai import AzureOpenAI
            client = AzureOpenAI(
                azure_endpoint=azure_endpoint,
                api_key=azure_key,
                api_version=os.environ.get("OPENAI_API_VERSION", "2024-10-21"),
            )
            data_uri = f"data:{media_type};base64,{b64}"
            resp = client.chat.completions.create(
                model=azure_dep,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri},
                            },
                            {
                                "type": "text",
                                "text": (
                                    "You are looking at the user's computer screen. "
                                    "Answer concisely for the ear (it will be spoken "
                                    "aloud): " + question
                                ),
                            },
                        ],
                    }
                ],
                max_completion_tokens=400,
            )
            ans = resp.choices[0].message.content
            return (ans or "").strip() or "I couldn't make sense of the screen."
        except Exception:
            pass

    import anthropic

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are looking at the user's computer screen. "
                            "Answer concisely for the ear (it will be spoken "
                            "aloud): " + question
                        ),
                    },
                ],
            }
        ],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
    return " ".join(parts).strip() or "I couldn't make sense of the screen."


def describe_screen(question: str, api_key: str) -> str:
    """Send the current screen to Claude vision and answer `question`
    about it. Used by Zoro's describe_screen tool."""
    b64, media_type = capture_screen_b64()
    return _vision_answer(b64, media_type, question, api_key)


# The web dev server serves web/public at the site root, so a PNG written
# here is reachable at /last_screen.png with no extra plumbing.
_SCREEN_PUBLIC = (
    Path(__file__).resolve().parent.parent / "web" / "public" / "last_screen.png"
)


def describe_screen_detailed(question: str, api_key: str) -> dict:
    """Like describe_screen, but also saves the captured screenshot to the
    web app's public dir so the view can show it. Returns
    {answer, image_url}. image_url is "" if the screenshot couldn't be
    saved (the spoken answer still works)."""
    import base64

    b64, media_type = capture_screen_b64()
    image_url = ""
    try:
        _SCREEN_PUBLIC.parent.mkdir(parents=True, exist_ok=True)
        _SCREEN_PUBLIC.write_bytes(base64.b64decode(b64))
        image_url = "/last_screen.png"
    except OSError:
        pass
    answer = _vision_answer(b64, media_type, question, api_key)
    return {"answer": answer, "image_url": image_url, "question": question}


# --- Computer control (state-changing — Zoro confirms first) -------------

_HOTKEYS = {
    "minimize": ("win", "down"),
    "maximize": ("win", "up"),
    "close": ("alt", "f4"),
    "show_desktop": ("win", "d"),
    "switch_window": ("alt", "tab"),
}


def type_text(text: str) -> None:
    import pyautogui

    pyautogui.write(text, interval=0.01)


def press_keys(keys: list[str]) -> None:
    """Press a key combo, e.g. ['ctrl','s'] or ['enter']."""
    import pyautogui

    if not keys:
        raise ValueError("no keys given")
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)


def mouse_click(x: int | None = None, y: int | None = None,
                 button: str = "left") -> None:
    import pyautogui

    if x is not None and y is not None:
        pyautogui.click(x=x, y=y, button=button)
    else:
        pyautogui.click(button=button)


def window_action(action: str) -> None:
    import pyautogui

    combo = _HOTKEYS.get(action)
    if combo is None:
        raise ValueError(f"unknown window action: {action}")
    pyautogui.hotkey(*combo)


def lock_screen() -> None:
    ctypes.windll.user32.LockWorkStation()


def sleep_pc() -> None:
    subprocess.Popen(
        ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
    )


# --- Deterministic desktop control (fast, no vision needed) --------------

_HOME = Path.home()
_KNOWN_FOLDERS = {
    "downloads": _HOME / "Downloads",
    "documents": _HOME / "Documents",
    "docs": _HOME / "Documents",
    "desktop": _HOME / "Desktop",
    "pictures": _HOME / "Pictures",
    "photos": _HOME / "Pictures",
    "music": _HOME / "Music",
    "videos": _HOME / "Videos",
    "home": _HOME,
    "user": _HOME,
}
# Where find_files looks when no explicit root is given.
_SEARCH_ROOTS = [
    _HOME / "Downloads",
    _HOME / "Documents",
    _HOME / "Desktop",
]
# Directories never worth walking for a user's document search.
_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "__pycache__", "AppData",
    "$RECYCLE.BIN", "System Volume Information",
}


def resolve_folder(target: str) -> Path:
    """Map a spoken folder name or path to an absolute Path.

    Accepts known names ("downloads", "desktop", ...) or any absolute /
    user-relative path ("~/Projects", "C:\\Tools").
    """
    key = target.strip().strip('"').lower()
    if key in _KNOWN_FOLDERS:
        return _KNOWN_FOLDERS[key]
    p = Path(target.strip().strip('"')).expanduser()
    return p


def open_folder(target: str) -> str:
    """Open a folder in File Explorer. Returns the resolved path."""
    p = resolve_folder(target)
    if not p.exists():
        raise FileNotFoundError(f"no folder '{target}' ({p})")
    subprocess.Popen(["explorer", str(p)])
    return str(p)


def open_path(path: str) -> str:
    """Open a file or folder with its default application."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")
    os.startfile(str(p))  # type: ignore[attr-defined]  # Windows-only
    return str(p)


def list_folder(target: str, limit: int = 40) -> list[str]:
    """Names of entries in a folder (folders first), capped at `limit`."""
    p = resolve_folder(target)
    if not p.is_dir():
        raise NotADirectoryError(f"not a folder: {p}")
    entries = sorted(
        p.iterdir(), key=lambda e: (e.is_file(), e.name.lower())
    )
    return [e.name + ("/" if e.is_dir() else "") for e in entries[:limit]]


def find_files(
    query: str, root: str | None = None, limit: int = 15
) -> list[str]:
    """Case-insensitive filename search. Searches Downloads/Documents/
    Desktop by default, or under `root` if given. Returns full paths,
    most-recently-modified first."""
    q = query.strip().strip('"').lower()
    roots = [resolve_folder(root)] if root else list(_SEARCH_ROOTS)
    hits: list[tuple[float, str]] = []
    cap = limit * 8  # enough matches to rank well; stop walking past this
    for base in roots:
        if not base.is_dir():
            continue
        stop = False
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith(".")
            ]
            for fn in filenames:
                if q in fn.lower():
                    fp = os.path.join(dirpath, fn)
                    try:
                        mtime = os.path.getmtime(fp)
                    except OSError:
                        mtime = 0.0
                    hits.append((mtime, fp))
                    if len(hits) >= cap:
                        stop = True
                        break
            if stop:  # bail out mid-directory, not only at dir boundaries
                break
    hits.sort(key=lambda t: t[0], reverse=True)
    return [p for _m, p in hits[:limit]]


def find_and_open(query: str, root: str | None = None) -> str:
    """Find the best filename match and open it. Returns the path opened."""
    matches = find_files(query, root, limit=1)
    if not matches:
        raise FileNotFoundError(f"no file matching '{query}'")
    return open_path(matches[0])


def copy_file(src: str, dst: str) -> str:
    import shutil

    s = Path(src).expanduser()
    d = Path(dst).expanduser()
    if d.is_dir():
        d = d / s.name
    shutil.copy2(s, d)
    return str(d)


def move_file(src: str, dst: str) -> str:
    import shutil

    s = Path(src).expanduser()
    d = Path(dst).expanduser()
    if d.is_dir():
        d = d / s.name
    shutil.move(str(s), str(d))
    return str(d)


def rename_file(src: str, new_name: str) -> str:
    s = Path(src).expanduser()
    d = s.with_name(new_name)
    s.rename(d)
    return str(d)


_TEXTLIKE = {
    ".txt", ".md", ".markdown", ".csv", ".log", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".html", ".css", ".ini", ".cfg", ".xml",
}


def read_text_file(path: str, max_chars: int = 4000) -> str:
    """Read a text-like file's contents (truncated). Binary docs like
    .pdf/.docx aren't read here — open them with open_path instead."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {p}")
    if p.suffix.lower() not in _TEXTLIKE:
        raise ValueError(
            f"{p.suffix or 'this file type'} isn't a plain-text file; "
            "I can open it but not read it aloud yet"
        )
    text = p.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars] + (" …(truncated)" if len(text) > max_chars else "")


_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _clip_apis():
    """user32/kernel32 with correct prototypes. Setting restype/argtypes
    matters: handles/pointers default to 32-bit c_int in ctypes and would
    be truncated on 64-bit Windows, corrupting the clipboard handle."""
    from ctypes import c_size_t, c_void_p, wintypes

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.restype = wintypes.BOOL
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL
    return u32, k32


def _open_clipboard(u32, attempts: int = 10, delay: float = 0.01) -> bool:
    """OpenClipboard fails transiently while another process holds the
    single global clipboard (clipboard-manager utilities poll it
    constantly). Retry briefly — ~100ms worst case — before giving up."""
    import time as _time

    for _ in range(attempts):
        if u32.OpenClipboard(None):
            return True
        _time.sleep(delay)
    return False


def clipboard_set(text: str) -> None:
    """Put text on the Windows clipboard.

    Pure Win32 via ctypes — thread-safe, so it works correctly when
    called from asyncio.to_thread (a Tk root must run on the main thread
    and was unreliable here). Raises OSError on any failure.
    """
    u32, k32 = _clip_apis()
    data = text or ""
    if not _open_clipboard(u32):
        raise OSError("could not open the clipboard")
    try:
        u32.EmptyClipboard()
        # Build the UTF-16 buffer first; its real byte size accounts for
        # surrogate pairs (astral chars like emoji take 2 UTF-16 units,
        # but Python len() counts them as 1) and the NUL terminator.
        buf = ctypes.create_unicode_buffer(data)
        size = ctypes.sizeof(buf)
        h_global = k32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not h_global:
            raise OSError("clipboard memory allocation failed")
        ptr = k32.GlobalLock(h_global)
        if not ptr:
            k32.GlobalFree(h_global)
            raise OSError("clipboard memory lock failed")
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            k32.GlobalUnlock(h_global)
        if not u32.SetClipboardData(_CF_UNICODETEXT, h_global):
            k32.GlobalFree(h_global)
            raise OSError("could not set clipboard data")
        # On success the OS takes ownership of h_global — must NOT free it.
    finally:
        u32.CloseClipboard()


def clipboard_get() -> str:
    """Read text from the Windows clipboard ('' if empty/non-text).

    Pure Win32 via ctypes — thread-safe. Never raises: any failure or a
    non-text clipboard yields ''.
    """
    try:
        u32, k32 = _clip_apis()
        if not u32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return ""
        if not _open_clipboard(u32):
            return ""
        try:
            handle = u32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                k32.GlobalUnlock(handle)
        finally:
            u32.CloseClipboard()
    except OSError:
        return ""
