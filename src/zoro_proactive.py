"""Zoro's proactive ambient loop — she speaks first.

Polls a few watchers (calendar, Gmail, durable reminders, a light end-of-day
nudge). When something is worth surfacing AND the safety layer allows an
interruption, it queues a one-line spoken directive and pokes the wake
service so the browser connects exactly like it does for "hey Zoro" — the
agent then opens the session with that line instead of a greeting.

  watcher -> safety gate -> data/pending_directive.json + wake poke
          -> browser connects -> agent speaks the directive

Run (normally started by the supervisor):
    .venv/Scripts/python.exe src/zoro_proactive.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import zoro_safety

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PENDING = DATA / "pending_directive.json"

POLL_SECONDS = 45
# If a directive queued in the last this-many seconds is still unconsumed,
# don't stack another on top of it.
PENDING_FRESH_SECONDS = 600
WAKE_WS = "ws://127.0.0.1:8765"


# --- pending directive + wake poke -----------------------------------------


def _pending_is_fresh() -> bool:
    try:
        d = json.loads(PENDING.read_text(encoding="utf-8"))
        return (time.time() - float(d.get("ts", 0))) < PENDING_FRESH_SECONDS
    except (OSError, ValueError):
        return False


def _poke_wake() -> None:
    try:
        from websockets.sync.client import connect

        with connect(WAKE_WS, open_timeout=3) as ws:
            ws.send('{"cmd": "proactive"}')
    except Exception:  # noqa: BLE001 - poke is best-effort
        pass


def _fire(text: str, key: str) -> bool:
    """Queue a spoken directive if the safety layer allows it now."""
    ok, _reason = zoro_safety.can_interrupt()
    if not ok:
        return False
    if zoro_safety.already_announced(key):
        return False
    if _pending_is_fresh():
        return False
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(
            json.dumps({"text": text, "ts": time.time(), "key": key}),
            encoding="utf-8",
        )
    except OSError:
        return False
    zoro_safety.mark_announced(key)
    zoro_safety.record_interruption()
    _poke_wake()
    return True


# --- watchers: each returns a list of (priority, text, dedupe_key) ----------
# lower priority number = more important (fired first)


def _sender_name(raw_from: str) -> str:
    raw = raw_from.strip()
    if "<" in raw:
        return raw.split("<", 1)[0].strip().strip('"') or raw
    return raw.split("@", 1)[0] if "@" in raw else raw


def _watch_reminders() -> list[tuple[int, str, str]]:
    """Return due, not-yet-fired reminders as candidates.

    We deliberately do NOT mark them fired here. A reminder is only
    consumed once it has actually been queued for speaking (see
    `_mark_reminder_fired`, called from `_cycle` after a successful
    `_fire`). Marking here would permanently drop any reminder the
    safety gate happened to block this cycle, or any second reminder
    that lost the one-per-cycle race.
    """
    import zoro_tools

    items = zoro_tools.read_reminders()
    now = time.time()
    out: list[tuple[int, str, str]] = []
    for r in items:
        if r.get("fired"):
            continue
        if float(r.get("due", 0)) <= now:
            out.append(
                (0, f"A reminder for you, Sir: {r.get('text','')}.",
                 f"rem:{r.get('id')}")
            )
    return out


def _mark_reminder_fired(rem_id: str) -> None:
    """Durably mark one reminder consumed — called ONLY after it has been
    queued for speaking, so a reminder is never lost without delivery.

    Delegates to zoro_tools, which serialises the read-modify-write with
    a cross-process lock and writes atomically, so this can never clobber
    a reminder the agent is adding concurrently.
    """
    import zoro_tools

    zoro_tools.mark_reminder_fired(rem_id)


def _watch_calendar(cfg: dict, account: str) -> list[tuple[int, str, str]]:
    import zoro_google

    thresholds = sorted(cfg.get("calendar_heads_up_minutes", [10, 2]))
    out: list[tuple[int, str, str]] = []
    for ev in zoro_google.upcoming_events(account, within_minutes=20):
        mins = ev["minutes_until"]
        if mins <= 0:
            continue
        for t in thresholds:
            if mins <= t:
                key = f"cal:{ev['id']}:{t}"
                if zoro_safety.already_announced(key):
                    continue
                n = max(1, round(mins))
                out.append(
                    (
                        1 + thresholds.index(t),
                        f"Heads up, Sir — \"{ev['summary']}\" starts in "
                        f"about {n} minute{'s' if n != 1 else ''}.",
                        key,
                    )
                )
                break
    return out


def _watch_gmail(cfg: dict, account: str) -> list[tuple[int, str, str]]:
    import zoro_google

    vips = [v.lower() for v in cfg.get("vip_senders", [])]
    keywords = [k.lower() for k in cfg.get("urgent_keywords", [])]
    out: list[tuple[int, str, str]] = []
    for m in zoro_google.unread_messages(account, max_results=8):
        frm = m["from"].lower()
        subj = m["subject"]
        is_vip = any(v in frm for v in vips)
        is_urgent = any(k in subj.lower() for k in keywords)
        if not (is_vip or is_urgent):
            continue
        out.append(
            (
                5,
                f"You have a{' priority' if is_vip else 'n urgent'} email "
                f"from {_sender_name(m['from'])}: {subj}.",
                f"mail:{m['id']}",
            )
        )
    return out


def _watch_tasks() -> list[tuple[int, str, str]]:
    """Pick up scheduled tasks whose due_at has passed. Each one becomes a
    high-priority directive telling Zoro to *carry it out* — the agent
    interprets the detail and uses her existing tools."""
    import zoro_tasks

    out: list[tuple[int, str, str]] = []
    for t in zoro_tasks.due_tasks():
        detail = (t.get("detail") or "").strip()
        body = (
            f"A scheduled task is now due — you queued this earlier for "
            f"yourself. Carry it out now using your tools. "
            f"Task: \"{t['title']}\"."
        )
        if detail:
            body += f" Details: {detail}"
        body += (
            " When the work is done (or as soon as you've handed back the "
            "outcome), call `mark_task_done` with this title so it leaves "
            "the queue."
        )
        out.append((0, body, f"task:{t['id']}"))
    return out


def _watch_end_of_day(cfg: dict) -> list[tuple[int, str, str]]:
    now = datetime.now()
    hour = int(cfg.get("ambient", {}).get("end_of_day_hour", 18))
    if now.hour < hour:
        return []
    key = f"eod:{now:%Y-%m-%d}"
    if zoro_safety.already_announced(key):
        return []
    return [
        (
            9,
            "It's wrapping-up time, Sir. Give a brief, warm end-of-day "
            "check-in: what's still open and anything on tomorrow you "
            "should flag. Keep it to a couple of sentences.",
            key,
        )
    ]


def _cycle() -> None:
    if _pending_is_fresh():
        # A directive is already queued and not yet consumed. The single
        # poke at _fire() time is fire-and-forget: if no browser was
        # connected/idle then (page in boot, in a call, or just closed),
        # that wake notification was lost and — with the freshness gate
        # below — NO new directive could be queued for 600s, so Zoro went
        # silent for ~10 min and the message often expired unseen.
        # Re-poke every cycle instead, so the directive is delivered the
        # moment a session becomes available, while still not stacking a
        # second directive on top of the unconsumed one.
        _poke_wake()
        return

    cfg = zoro_safety.load_config()
    pro = cfg.get("proactive", {})
    account = cfg.get("gmail_account", "")
    candidates: list[tuple[int, str, str]] = []

    if pro.get("reminders", True):
        candidates += _watch_reminders()
    if pro.get("tasks", True):
        try:
            candidates += _watch_tasks()
        except Exception as e:  # noqa: BLE001 - one bad watcher mustn't kill loop
            print(f"[proactive] task watcher error: {e}", flush=True)
    if account and pro.get("calendar", True):
        try:
            candidates += _watch_calendar(cfg, account)
        except Exception as e:  # noqa: BLE001 - one bad watcher mustn't kill loop
            print(f"[proactive] calendar watcher error: {e}", flush=True)
    if account and pro.get("gmail", True):
        try:
            candidates += _watch_gmail(cfg, account)
        except Exception as e:  # noqa: BLE001
            print(f"[proactive] gmail watcher error: {e}", flush=True)
    if pro.get("ambient_nudges", True):
        candidates += _watch_end_of_day(cfg)

    if not candidates:
        return
    # one interruption per cycle — most important first
    candidates.sort(key=lambda c: c[0])
    priority, text, key = candidates[0]
    if _fire(text, key):
        # Only now that the reminder has actually been queued for
        # speaking do we consume it. Reminders the gate blocked, or
        # that lost the one-per-cycle race, stay pending and re-surface
        # next cycle instead of being silently dropped.
        if key.startswith("rem:"):
            _mark_reminder_fired(key[len("rem:"):])
        elif key.startswith("task:"):
            import zoro_tasks
            try:
                zoro_tasks.mark_fired(int(key[len("task:"):]))
            except (ValueError, Exception) as e:  # noqa: BLE001
                print(f"[proactive] task mark_fired failed: {e}", flush=True)
        print(f"[proactive] fired ({key}): {text}", flush=True)


def main() -> None:
    print("[proactive] watching (calendar, gmail, reminders, end-of-day)",
          flush=True)
    while True:
        try:
            _cycle()
        except Exception as e:  # noqa: BLE001 - never let the loop die
            print(f"[proactive] cycle error: {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
