"""Shared safety / gating layer for Zoro's autonomous behaviour.

Every proactive interruption and every state-changing action routes through
here. It is imported by the agent (inside a LiveKit job), the proactive
service, and the supervisor/tray, so it stays dependency-free and tolerant of
concurrent access from several processes.

Two files under ./data:
  - zoro_config.json   user-editable settings (quiet hours, limits, VIPs)
  - zoro_runtime.json  mutable state (stand-down, interruption log, dedupe)

The split means editing settings never clobbers runtime state and vice versa.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

try:
    import redis  # type: ignore
except ImportError:
    redis = None  # type: ignore

_redis_client = None
_redis_last_check = 0.0


def _get_redis():
    """Retrieve shared Redis client for distributed multi-device state sync."""
    global _redis_client, _redis_last_check
    now = time.time()
    if _redis_client is not None:
        return _redis_client
    if now - _redis_last_check < 30.0:
        return None
    _redis_last_check = now
    if redis is None:
        return None
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        client.ping()
        _redis_client = client
        return _redis_client
    except Exception:
        _redis_client = None
        return None

_DATA = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = _DATA / "zoro_config.json"
RUNTIME_PATH = _DATA / "zoro_runtime.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "max_interruptions_per_hour": 5,
    "proactive": {
        "calendar": True,
        "gmail": True,
        "reminders": True,
        "ambient_nudges": True,
    },
    "calendar_heads_up_minutes": [10, 2],
    "gmail_account": "",
    "vip_senders": [],
    "urgent_keywords": ["urgent", "asap", "important", "deadline", "today"],
    "ambient": {"long_focus_minutes": 90, "end_of_day_hour": 18},
}

_DEFAULT_RUNTIME: dict[str, Any] = {
    # epoch seconds; proactive is suppressed until this time. 0 = active.
    "stand_down_until": 0,
    # list of epoch seconds, one per spoken interruption (pruned to 1h).
    "interruptions": [],
    # dedupe map: key -> epoch seconds it was last announced.
    "announced": {},
}

# State-changing actions Zoro must verbally confirm before performing.
# Read-only/low-risk actions (time, weather, search, describe_screen) are
# deliberately absent — those just run.
CONFIRM_ACTIONS = frozenset(
    {
        "send_email",
        "delete",
        "overwrite_file",
        "type_text",
        "press_keys",
        "click",
        "manage_window",
        "sleep_computer",
        "change_setting",
    }
)


def requires_confirmation(action: str) -> bool:
    """True if `action` must be confirmed by the user before running."""
    return action in CONFIRM_ACTIONS


# --- low-level json io (atomic, race-tolerant) -----------------------------


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(default)
        return data
    except (OSError, ValueError):
        return dict(default)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON, raising OSError if it genuinely fails.

    Previously this swallowed every OSError, so a failed save (e.g. a
    transient Windows os.replace PermissionError while another process had
    the file open) silently no-op'd — stand-down wasn't persisted, the
    dedupe map was lost (repeat nags), and the interruption rate limit
    wasn't enforced. Now: retry the sub-millisecond Windows contention
    window like zoro_tools does, and propagate any error that doesn't
    clear so callers can decide what to do.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # On Windows os.replace raises PermissionError if another process
        # has the destination open — readers do, very briefly. Retry the
        # ~1s worst-case contention window before giving up.
        for attempt in range(50):
            try:
                os.replace(tmp, path)
                tmp = None
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.02)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def load_config() -> dict[str, Any]:
    """User settings, with any missing keys filled from defaults."""
    cfg = _read_json(CONFIG_PATH, _DEFAULT_CONFIG)
    merged = dict(_DEFAULT_CONFIG)
    merged.update(cfg)
    # one level of nested-dict merge is enough for our shape
    for k, v in _DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            sub = dict(v)
            sub.update(cfg.get(k, {}) or {})
            merged[k] = sub
    return merged


def _load_runtime() -> dict[str, Any]:
    rt = _read_json(RUNTIME_PATH, _DEFAULT_RUNTIME)
    for k, v in _DEFAULT_RUNTIME.items():
        rt.setdefault(k, v if not isinstance(v, (list, dict)) else type(v)())
    return rt


def _save_runtime(rt: dict[str, Any]) -> None:
    _write_json(RUNTIME_PATH, rt)


# --- cross-process runtime lock --------------------------------------------
# zoro_runtime.json is read-modify-written from several processes at once
# (the agent inside a LiveKit job, the proactive loop, the supervisor/tray).
# Atomic replace alone stops *torn reads* but not *lost updates*: two
# processes that each load -> mutate -> save concurrently clobber each
# other, dropping `announced` entries (repeat proactive nags) or
# `interruptions` entries (rate limit bypassed). This mutex serialises the
# whole read-modify-write across processes — same pattern zoro_tools uses
# for reminders.json. A lock orphaned by a crashed holder is reclaimed
# after _LOCK_STALE.

_LOCK_TIMEOUT = 5.0
_LOCK_STALE = 30.0


@contextlib.contextmanager
def _runtime_lock():
    lock = RUNTIME_PATH.with_name(RUNTIME_PATH.name + ".lock")
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + _LOCK_TIMEOUT
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except (FileExistsError, PermissionError):
            # FileExistsError: lock held. PermissionError: Windows returns
            # ACCESS_DENIED (not EEXIST) when the lock file is mid-unlink
            # by the previous holder — also just contention, retry.
            try:
                if time.time() - os.path.getmtime(lock) > _LOCK_STALE:
                    os.unlink(lock)  # previous holder crashed — reclaim
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise TimeoutError("zoro_runtime.json is locked")
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


def _update_runtime(mutate):
    """Lock-serialised load -> mutate -> save of the runtime file.

    `mutate(rt)` may change `rt` in place and/or return a value, which is
    returned to the caller. The whole cycle holds the cross-process lock
    so a concurrent writer in another process can't clobber the update.
    """
    with _runtime_lock():
        rt = _load_runtime()
        result = mutate(rt)
        _save_runtime(rt)
        return result


# --- quiet hours -----------------------------------------------------------


def _parse_hhmm(s: str, fallback: dtime) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


def in_quiet_hours(now: datetime | None = None) -> bool:
    cfg = load_config()
    qh = cfg.get("quiet_hours") or {}
    start = _parse_hhmm(qh.get("start", "22:00"), dtime(22, 0))
    end = _parse_hhmm(qh.get("end", "07:00"), dtime(7, 0))
    t = (now or datetime.now()).time()
    if start <= end:
        return start <= t < end
    # window crosses midnight (e.g. 22:00 -> 07:00)
    return t >= start or t < end


# --- stand-down ("Zoro, stand down" / "resume") ----------------------------


def set_stand_down(on: bool, minutes: int | None = None) -> None:
    """Suspend (or resume) all proactive interruptions and autonomous actions.

    `minutes` optionally auto-resumes after a period; otherwise stand-down
    lasts until explicitly resumed.
    State is persisted to Azure Redis for multi-device cross-sync and mirrored
    to the local runtime file.
    """
    r = _get_redis()
    if r:
        try:
            if not on:
                r.delete("zoro:safety:stand_down_until")
            elif minutes:
                until = int(time.time()) + minutes * 60
                r.set("zoro:safety:stand_down_until", str(until), ex=minutes * 60 + 3600)
            else:
                r.set("zoro:safety:stand_down_until", str(2**31 - 1))
        except Exception:
            pass

    def _m(rt: dict[str, Any]) -> None:
        if not on:
            rt["stand_down_until"] = 0
        elif minutes:
            rt["stand_down_until"] = int(time.time()) + minutes * 60
        else:
            rt["stand_down_until"] = 2**31 - 1  # effectively indefinite

    _update_runtime(_m)


def is_stood_down() -> bool:
    r = _get_redis()
    if r:
        try:
            val = r.get("zoro:safety:stand_down_until")
            if val is not None:
                until = int(val)
                if until and time.time() < until:
                    return True
                if until and time.time() >= until and until != 0:
                    r.delete("zoro:safety:stand_down_until")
                    return False
                return False
        except Exception:
            pass

    def _m(rt: dict[str, Any]) -> bool:
        until = rt.get("stand_down_until", 0)
        if until and time.time() < until:
            return True
        if until and time.time() >= until and until != 0:
            rt["stand_down_until"] = 0  # auto-resume expired stand-down
        return False

    try:
        return _update_runtime(_m)
    except (OSError, TimeoutError):
        # Best-effort: a lock timeout / write hiccup must not make a
        # status check throw. Fall back to a lock-free read.
        rt = _load_runtime()
        until = rt.get("stand_down_until", 0)
        return bool(until and time.time() < until)


# --- interruption rate limiting & dedupe -----------------------------------


def _prune_interruptions(rt: dict[str, Any]) -> list[float]:
    cutoff = time.time() - 3600
    kept = [t for t in rt.get("interruptions", []) if t >= cutoff]
    rt["interruptions"] = kept
    return kept


def can_interrupt(now: datetime | None = None) -> tuple[bool, str]:
    """Whether Zoro may speak unprompted right now. Returns (ok, reason)."""
    if is_stood_down():
        return False, "stood down"
    if in_quiet_hours(now):
        return False, "quiet hours"

    limit = int(load_config().get("max_interruptions_per_hour", 5))
    r = _get_redis()
    if r:
        try:
            t_now = time.time()
            r.zremrangebyscore("zoro:safety:interruptions", "-inf", t_now - 3600)
            count = r.zcard("zoro:safety:interruptions")
            if count >= limit:
                return False, "hourly interruption limit reached"
            return True, "ok"
        except Exception:
            pass

    try:
        recent = _update_runtime(_prune_interruptions)
    except (OSError, TimeoutError):
        recent = _prune_interruptions(_load_runtime())
    if len(recent) >= limit:
        return False, "hourly interruption limit reached"
    return True, "ok"


def record_interruption() -> None:
    r = _get_redis()
    if r:
        try:
            t_now = time.time()
            r.zremrangebyscore("zoro:safety:interruptions", "-inf", t_now - 3600)
            r.zadd("zoro:safety:interruptions", {f"{t_now}": t_now})
            r.expire("zoro:safety:interruptions", 7200)
        except Exception:
            pass

    def _m(rt: dict[str, Any]) -> None:
        _prune_interruptions(rt)
        rt["interruptions"].append(time.time())

    try:
        _update_runtime(_m)
    except Exception:
        pass


def already_announced(key: str, ttl_seconds: int = 6 * 3600) -> bool:
    """True if `key` was announced within ttl — prevents repeat nags for the
    same calendar event / email / reminder."""
    r = _get_redis()
    if r:
        try:
            val = r.get(f"zoro:safety:announced:{key}")
            if val is not None:
                return (time.time() - float(val)) < ttl_seconds
        except Exception:
            pass

    rt = _load_runtime()
    announced = rt.get("announced", {})
    ts = announced.get(key)
    return ts is not None and (time.time() - ts) < ttl_seconds


def mark_announced(key: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.set(f"zoro:safety:announced:{key}", str(time.time()), ex=86400)
        except Exception:
            pass

    def _m(rt: dict[str, Any]) -> None:
        announced = rt.setdefault("announced", {})
        announced[key] = time.time()
        # keep the dedupe map from growing unbounded
        cutoff = time.time() - 24 * 3600
        rt["announced"] = {k: v for k, v in announced.items() if v >= cutoff}

    try:
        _update_runtime(_m)
    except Exception:
        pass


# --- multi-device wake & turn arbitration ----------------------------------


def claim_turn(
    device_id: str,
    volume_rms: float = -20.0,
    is_active: bool = False,
    window_ms: int = 150,
) -> tuple[bool, str]:
    """Arbitrate between nearby devices when the wake word is detected.

    When both laptop and phone hear "Hey Zoro", both call claim_turn().
    They register a candidate bid with their audio volume and activity status.
    The device with the highest score (higher RMS loudness, active screen priority)
    wins the turn. Other devices yield silently.
    Returns (won_turn: bool, reason: str).
    """
    r = _get_redis()
    if not r:
        # Standalone local fallback: if Redis is unavailable, single device always wins
        return True, "redis_offline_standalone"

    try:
        # 1. Check if a turn is already currently active and held by someone else
        active = r.get("zoro:turn:active")
        if active and active != device_id:
            return False, f"yielded: active turn held by {active}"

        # 2. Calculate priority score:
        # volume_rms is typically negative dB (-40 dB quiet, -10 dB loud).
        # Being in active use (e.g. laptop unlocked / typing) adds a +15 dB bonus.
        score = float(volume_rms) + (15.0 if is_active else 0.0)

        # 3. Register bid into candidates sorted set with 2s expiry
        r.zadd("zoro:turn:candidates", {device_id: score})
        r.expire("zoro:turn:candidates", 2)

        # 4. Wait out the arbitration window (e.g. 150ms) to allow nearby device's bid to arrive
        time.sleep(window_ms / 1000.0)

        # 5. Determine the winner (highest score)
        candidates = r.zrevrange("zoro:turn:candidates", 0, 0)
        if not candidates:
            winner = device_id
        else:
            winner = candidates[0]

        if winner == device_id:
            # We won! Lock active turn for 10 seconds (refreshed by speech)
            r.set("zoro:turn:active", device_id, ex=10)
            # Clear candidates
            r.delete("zoro:turn:candidates")
            return True, "elected"
        else:
            return False, f"yielded: {winner} has higher priority / volume"
    except Exception as e:
        # On any redis glitch, default to true so voice is never blocked
        return True, f"fallback_on_error: {e}"


def release_turn(device_id: str) -> None:
    """Release the active turn once Zoro finishes speaking or user finishes utterance."""
    r = _get_redis()
    if r:
        try:
            current = r.get("zoro:turn:active")
            if current == device_id:
                r.delete("zoro:turn:active")
        except Exception:
            pass


def get_active_turn() -> str | None:
    """Check which device currently holds the active turn."""
    r = _get_redis()
    if r:
        try:
            return r.get("zoro:turn:active")
        except Exception:
            pass
    return None


# --- proactive ambient directive state (Azure Redis + local mirror) --------

PENDING_DIRECTIVE_FILE = _DATA / "pending_directive.json"


def set_pending_directive(text: str, key: str, ttl_seconds: int = 600) -> bool:
    """Store a pending proactive directive in Azure Redis for multi-device delivery,
    publishing to the Redis events channel, and mirroring to the local file."""
    payload = json.dumps({"text": text, "ts": time.time(), "key": key})
    r = _get_redis()
    if r:
        try:
            r.set("zoro:proactive:pending", payload, ex=ttl_seconds)
            r.publish("zoro:proactive:events", json.dumps({"event": "proactive", "text": text, "key": key}))
        except Exception:
            pass

    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        PENDING_DIRECTIVE_FILE.write_text(payload, encoding="utf-8")
    except OSError:
        pass
    return True


def is_pending_directive_fresh(max_age_seconds: int = 600) -> bool:
    """Check if an unconsumed proactive directive is still active."""
    r = _get_redis()
    if r:
        try:
            raw = r.get("zoro:proactive:pending")
            if raw:
                d = json.loads(raw)
                return (time.time() - float(d.get("ts", 0))) < max_age_seconds
        except Exception:
            pass

    try:
        if PENDING_DIRECTIVE_FILE.exists():
            d = json.loads(PENDING_DIRECTIVE_FILE.read_text(encoding="utf-8"))
            return (time.time() - float(d.get("ts", 0))) < max_age_seconds
    except (OSError, ValueError):
        pass
    return False


def consume_pending_directive() -> tuple[str | None, str | None]:
    """Retrieve and clear a pending proactive directive across devices.
    Returns (instruction_for_llm, raw_text_for_ui)."""
    data = None
    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            pipe.get("zoro:proactive:pending")
            pipe.delete("zoro:proactive:pending")
            res = pipe.execute()
            if res and res[0]:
                data = json.loads(res[0])
        except Exception:
            data = None

    if not data:
        try:
            if PENDING_DIRECTIVE_FILE.exists():
                data = json.loads(PENDING_DIRECTIVE_FILE.read_text(encoding="utf-8"))
                try:
                    PENDING_DIRECTIVE_FILE.unlink()
                except OSError:
                    pass
        except (OSError, ValueError):
            return None, None

    try:
        if PENDING_DIRECTIVE_FILE.exists():
            PENDING_DIRECTIVE_FILE.unlink()
    except OSError:
        pass

    if not data:
        return None, None

    ts = float(data.get("ts", 0))
    if time.time() - ts > 600:
        return None, None
    text = (data.get("text") or "").strip()
    if not text:
        return None, None
    return (
        f'A proactive directive is pending. Deliver it naturally and concisely as your opening remark: "{text}". Then invite a reply.',
        text,
    )

