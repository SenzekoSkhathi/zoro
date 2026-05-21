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
    """
    def _m(rt: dict[str, Any]) -> None:
        if not on:
            rt["stand_down_until"] = 0
        elif minutes:
            rt["stand_down_until"] = int(time.time()) + minutes * 60
        else:
            rt["stand_down_until"] = 2**31 - 1  # effectively indefinite

    # User-initiated and important: if it can't be persisted the caller
    # should hear about it rather than silently stay in the wrong state.
    _update_runtime(_m)


def is_stood_down() -> bool:
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
    try:
        recent = _update_runtime(_prune_interruptions)
    except (OSError, TimeoutError):
        # Best-effort housekeeping: if the lock/write is briefly
        # unavailable, still evaluate the limit on a lock-free read.
        recent = _prune_interruptions(_load_runtime())
    limit = int(load_config().get("max_interruptions_per_hour", 5))
    if len(recent) >= limit:
        return False, "hourly interruption limit reached"
    return True, "ok"


def record_interruption() -> None:
    def _m(rt: dict[str, Any]) -> None:
        _prune_interruptions(rt)
        rt["interruptions"].append(time.time())

    _update_runtime(_m)


def already_announced(key: str, ttl_seconds: int = 6 * 3600) -> bool:
    """True if `key` was announced within ttl — prevents repeat nags for the
    same calendar event / email / reminder."""
    rt = _load_runtime()
    announced = rt.get("announced", {})
    ts = announced.get(key)
    return ts is not None and (time.time() - ts) < ttl_seconds


def mark_announced(key: str) -> None:
    def _m(rt: dict[str, Any]) -> None:
        announced = rt.setdefault("announced", {})
        announced[key] = time.time()
        # keep the dedupe map from growing unbounded
        cutoff = time.time() - 24 * 3600
        rt["announced"] = {k: v for k, v in announced.items() if v >= cutoff}

    _update_runtime(_m)
