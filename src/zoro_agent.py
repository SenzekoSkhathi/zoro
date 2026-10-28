"""Zoro — voice-first assistant agent.

Pipeline:
  mic -> Azure Speech (STT) -> Azure OpenAI (brain) -> Azure Neural (TTS) -> speakers

Run locally in console mode (no phone / web UI needed yet):
    .venv/Scripts/python.exe src/zoro_agent.py console
"""

import asyncio
import functools
import importlib
import inspect
import json
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Windows consoles default to cp1252; LiveKit's CLI prints an emoji on
# startup which raises UnicodeEncodeError and kills the process before the
# agent ever runs. Force UTF-8 on the std streams so plain `python
# src/zoro_agent.py console` works without a PYTHONUTF8 prefix.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    except (AttributeError, ValueError):
        pass

import httpx
from dotenv import load_dotenv
from livekit.agents import (  # pyright: ignore[reportMissingImports]
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)
from livekit.plugins import (  # pyright: ignore[reportMissingImports]
    anthropic,
    azure,
    openai,
    silero,
)

import zoro_browser  # pyright: ignore[reportMissingImports]
import zoro_claude_code # pyright: ignore[reportMissingImports]
import zoro_computer # pyright: ignore[reportMissingImports]
import zoro_corrections # pyright: ignore[reportMissingImports]
import zoro_episodes # pyright: ignore[reportMissingImports]
import zoro_lessons  # pyright: ignore[reportMissingImports]
import zoro_reflector  # pyright: ignore[reportMissingImports]
import zoro_github # pyright: ignore[reportMissingImports]
import zoro_google # pyright: ignore[reportMissingImports]
import zoro_memory  # pyright: ignore[reportMissingImports]
import zoro_safety  # pyright: ignore[reportMissingImports]
import zoro_shell  # pyright: ignore[reportMissingImports]
import zoro_retry  # pyright: ignore[reportMissingImports]
import zoro_spotify  # pyright: ignore[reportMissingImports]
import zoro_tasks # pyright: ignore[reportMissingImports]
import zoro_tools  # pyright: ignore[reportMissingImports]
import zoro_whatsapp # pyright: ignore[reportMissingImports]
import zoro_youtube # pyright: ignore[reportMissingImports]
import zoro_telemetry # pyright: ignore[reportMissingImports]
import zoro_schema_cache # pyright: ignore[reportMissingImports]
from zoro_prompt import ZORO_SYSTEM_PROMPT  # pyright: ignore[reportMissingImports]

zoro_schema_cache.install_schema_cache()
zoro_telemetry.init_telemetry()

# Session id for episode rows — lets us collate a single conversation later.
_SESSION_ID = uuid.uuid4().hex[:12]

# Recent successful tool episodes a follow-up utterance could be correcting.
# Capped tiny — corrections target the *last* action almost always. Each
# entry: {"id": int, "tool": str, "result": str, "ts": float}.
_RECENT_EPISODES: list[dict] = []
_RECENT_MAX = 4


def _register_episode(ep: dict) -> None:
    _RECENT_EPISODES.append(ep)
    if len(_RECENT_EPISODES) > _RECENT_MAX:
        del _RECENT_EPISODES[: -_RECENT_MAX]


def _consume_correctable_episode() -> dict | None:
    """Pop the most recent still-correctable episode (within the window and
    not yet flagged) and return it. Returns None if there isn't one."""
    now = time.monotonic()
    while _RECENT_EPISODES:
        ep = _RECENT_EPISODES[-1]
        # Stale entries get evicted but we don't keep looking past them —
        # corrections only target the immediately previous action.
        if now - ep["ts"] > zoro_corrections.CORRECTION_WINDOW_SECONDS:
            _RECENT_EPISODES.clear()
            return None
        return ep
    return None

# Load Silero VAD once at module import (~200-500ms) instead of per session.
# The worker stays warm via num_idle_processes=1, so sessions reuse this.
# Tuned to reject background noise / audio artifacts: a higher activation
# threshold (default 0.5 → 0.65) ignores low-confidence frames, and a
# longer min_speech_duration (default 50ms → 150ms) drops clicks, coughs,
# and short bursts that aren't real speech.
_VAD = silero.VAD.load(
    activation_threshold=0.65,
    min_speech_duration=0.15,
    min_silence_duration=0.55,
)


def build_instructions() -> str:
    """System prompt + the user's profile and recent memories from Supabase.

    Injected once per session so Zoro always 'knows' the user with no
    lookup lag. Failures are non-fatal — she just runs without memory.
    """
    instructions = ZORO_SYSTEM_PROMPT
    # Anchor Zoro to the actual wall clock — without this she has no sense
    # of time and defaults to "good morning" regardless of when the user is
    # talking to her. Captured fresh on every session.
    now = datetime.now()
    instructions += (
        "\n\n# Current local time (session start)\n"
        f"{now.strftime('%A, %B %d, %Y — %I:%M %p')} "
        f"(24h: {now.strftime('%H:%M')}).\n"
        "Use this for any time-aware reply. Greet according to the hour:\n"
        "- 05:00–11:59 → \"Good morning\"\n"
        "- 12:00–16:59 → \"Good afternoon\"\n"
        "- 17:00–20:59 → \"Good evening\"\n"
        "- 21:00–04:59 → \"Good evening\" (or just acknowledge it's late)\n"
        "Never say \"good morning\" outside the morning hours above. If a "
        "long session has run and you need a fresher reading, call "
        "`get_current_time` rather than guessing."
    )
    try:
        profile = zoro_memory.get_profile()
        if profile.get("about"):
            name = profile.get("name") or "the user"
            instructions += (
                f"\n\n# Profile of {name} (long-term, you know this)\n"
                f"{profile['about']}"
            )
        memories = zoro_memory.recent_memories(8)
        if memories:
            joined = "\n".join(f"- {mtxt}" for mtxt in memories)
            instructions += (
                "\n\n# Recently remembered (may have changed since)\n"
                f"{joined}"
            )
    except Exception as e:  # noqa: BLE001 - memory is best-effort
        print(f"[zoro] memory load skipped: {e}")
    try:
        instructions += _similar_episodes_block()
    except Exception as e:  # noqa: BLE001 - episodes are best-effort
        print(f"[zoro] episodes load skipped: {e}")
    return instructions

# Friendly spoken names -> what Windows `start` should launch.
APP_ALIASES = {
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "paint": "mspaint",
    "file explorer": "explorer",
    "explorer": "explorer",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "browser": "msedge",
    "settings": "ms-settings:",
    "camera": "microsoft.windows.camera:",
}

# Load .env that sits next to the project root (one level up from src/).
# override=True is required: the shell may pre-set empty keys (e.g. an empty
# ANTHROPIC_API_KEY), and without override dotenv keeps the empty value.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

# Topic the web view subscribes to for visual content (see
# web/components/ZoroDisplay.tsx). Voice stays primary; this is the
# screen-second channel that shows the detail Zoro doesn't read aloud.
DISPLAY_TOPIC = "zoro.display"


async def publish_display(kind: str, data: dict, *, refresh: bool = False) -> None:
    """Push a content card to the web view over the LiveKit data channel.

    `refresh=True` marks the frame as a live update: the web view refreshes
    the card in place if it's already on screen but does NOT open it if the
    user has dismissed it. Used by the Spotify watcher to keep the
    now-playing card in sync without hijacking the display.

    Best-effort: a missing room (console mode) or any transport hiccup
    must never break the spoken reply, so all failures are swallowed.
    """
    try:
        room = get_job_context().room
        frame: dict = {"kind": kind, "data": data}
        if refresh:
            frame["refresh"] = True
        payload = json.dumps(frame)
        await room.local_participant.publish_data(
            payload, reliable=True, topic=DISPLAY_TOPIC
        )
        print(
            f"[zoro] display published: kind={kind} "
            f"bytes={len(payload)} room={room.name}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001 - display is non-essential
        print(f"[zoro] display publish skipped: {e}", flush=True)


def _notes_panel_data() -> dict:
    """Build the notes & reminders panel payload: recent notes plus
    pending (not-yet-fired) reminders with their absolute due time in
    epoch milliseconds so the web view can count down live."""
    notes = []
    for raw in zoro_tools.read_notes(8):
        # Stored as "[YYYY-MM-DD HH:MM] text"; split for nicer display.
        if raw.startswith("[") and "] " in raw:
            stamp, _, text = raw[1:].partition("] ")
            notes.append({"stamp": stamp, "text": text})
        else:
            notes.append({"stamp": "", "text": raw})
    reminders = [
        {"text": r.get("text", ""), "due_ms": int(float(r.get("due", 0)) * 1000)}
        for r in zoro_tools.read_reminders()
        if not r.get("fired")
    ]
    reminders.sort(key=lambda r: r["due_ms"])
    return {"notes": notes, "reminders": reminders}


async def publish_notes_panel() -> None:
    """Publish the notes & reminders panel; best-effort like all display."""
    try:
        data = await asyncio.to_thread(_notes_panel_data)
    except Exception as e:  # noqa: BLE001 - display is non-essential
        print(f"[zoro] notes panel skipped: {e}")
        return
    await publish_display("notes", data)


def _summarise_claude_result(state: dict) -> str:
    """One spoken sentence summarising how an autonomous engineering session ended.

    Trims long results so Zoro doesn't read paragraphs aloud — the full
    text stays on the card for the user to read.
    """
    raw = (state.get("result") or "").strip().replace("\n", " ")
    merge = state.get("merge") or ""
    if state.get("status") == "failed":
        snippet = raw[:140] + ("…" if len(raw) > 140 else "")
        return f"The engineering task couldn't finish: {snippet}" if snippet else (
            "The engineering task hit an error."
        )
    if merge == "rolled-back":
        return (
            "The autonomous change broke my boot check, so I rolled it back. "
            "Nothing was merged, Sir."
        )
    if merge == "no-changes":
        return "The autonomous engineering core ran but didn't change anything."
    snippet = raw[:200] + ("…" if len(raw) > 200 else "")
    suffix = " Merged into main." if merge == "merged" else ""
    if snippet:
        return f"Task complete Sir. {snippet}{suffix}"
    return "Task is complete Sir." + suffix


async def _speak_or_queue(session, text: str) -> None:
    """Speak via the live session, or fall back to a proactive directive
    if the session has ended before Claude finished."""
    try:
        if session is not None:
            await session.say(text)
            return
    except Exception:  # noqa: BLE001 - fall through to proactive queue
        pass
    try:
        import zoro_proactive  # pyright: ignore[reportMissingImports]

        zoro_proactive._fire(text, f"claude_done_{int(time.time())}")
    except Exception as e:  # noqa: BLE001
        print(f"[zoro] claude announce skipped: {e}", flush=True)


async def _run_claude_task(
    task: str, session=None, role: str | None = None,
) -> None:
    """Stream a Claude Code session into the web view as it runs, then
    speak a one-line summary when it finishes.

    The card replaces in place on each event; a single "claude" widget
    holds the latest task and its progress log.

    Phase 3: writes a `kind='sub_agent'` (or `kind='claude_code'` for
    ungated runs) episode at the end so sub-agent quality is measurable
    by the same telemetry stack as every other tool. Without this the
    fire-and-forget runs were invisible to the reflector.
    """
    t0 = time.monotonic()
    state: dict = {
        "task": task,
        "role": role or "",
        "status": "running",
        "lines": [],
        "result": "",
    }
    await publish_display("claude", state)
    err_exc: str | None = None
    try:
        async for ev in zoro_claude_code.run_streaming(task, role=role):
            kind = ev["kind"]
            if kind == "result":
                state["status"] = "done" if ev.get("ok") else "failed"
                state["result"] = ev.get("text", "")
            elif kind == "error":
                state["status"] = "failed"
                state["result"] = ev.get("text", "")
            elif kind == "tool":
                state["lines"].append(f"→ {ev['text']}")
            elif kind == "narration":
                state["lines"].append(ev["text"])
            elif kind == "merge":
                state["merge"] = ev.get("status", "")
                state["lines"].append(f"⎇ {ev.get('text', '')}")
            state["lines"] = state["lines"][-30:]
            await publish_display("claude", state)
    except Exception as e:  # noqa: BLE001 - background, must not crash
        state["status"] = "failed"
        state["result"] = str(e)
        err_exc = f"{type(e).__name__}: {e}"
        await publish_display("claude", state)

    # Map the streamed final state to an episode outcome. Merge status
    # is the strongest signal when present (merged/rolled-back/no-changes
    # are all distinguishable); fall back to the run's done/failed flag.
    merge_status = state.get("merge")
    if merge_status == "merged":
        outcome = "success"
    elif merge_status == "rolled-back":
        outcome = "error"
    elif merge_status in ("no-changes", "skipped"):
        outcome = "aborted"
    else:
        outcome = "success" if state["status"] == "done" else "error"

    asyncio.create_task(asyncio.to_thread(
        zoro_episodes.log_episode,
        kind="sub_agent" if role else "claude_code",
        tool=role or "claude_code",
        task=task[:300],
        args={"task": task, "role": role or ""},
        outcome=outcome,
        latency_ms=int((time.monotonic() - t0) * 1000),
        result_summary=(state.get("result") or "")[:300],
        error=err_exc,
        session_id=_SESSION_ID,
    ))

    await _speak_or_queue(session, _summarise_claude_result(state))


async def publish_now_playing(delay: float = 0.0) -> None:
    """Push the current Spotify state to the web view.

    `delay` lets us wait briefly after a state-changing call (Spotify takes
    a few hundred ms to reflect a new track / paused state on the API).
    Best-effort; failures never break the spoken reply.
    """
    if delay:
        await asyncio.sleep(delay)
    try:
        cur = await asyncio.to_thread(zoro_spotify.now_playing)
    except Exception as e:  # noqa: BLE001 - display is non-essential
        print(f"[zoro] nowplaying publish skipped: {e}")
        return
    await publish_display("nowplaying", cur or {"empty": True})


# How often the Spotify watcher polls, in seconds. Idle = nothing playing
# (poll slowly to spare the API); active = a track is playing; near-end
# tightens so an automatic roll-over to the next queued song is reflected
# within a beat instead of up to a full active-poll later.
_SPOTIFY_POLL_IDLE = 15.0
_SPOTIFY_POLL_ACTIVE = 4.0
_SPOTIFY_POLL_NEAR_END = 1.5
_SPOTIFY_NEAR_END_MS = 6000


def _now_playing_sig(cur: dict | None) -> tuple:
    """Fingerprint of the state that should trigger a fresh push: which
    track, and whether it's playing. Progress is deliberately excluded —
    the web view ticks that locally between pushes, so including it would
    make every poll look like a change."""
    if not cur:
        return ("none", False)
    return (
        cur.get("track_url") or cur.get("track") or "",
        bool(cur.get("is_playing")),
    )


async def _watch_spotify() -> None:
    """Keep the now-playing card in sync with what Spotify is actually
    doing, without Zoro having to be asked.

    Zoro otherwise only refreshes the card when she runs a Spotify tool, so
    the common cases — a song ending and the queue advancing, or the user
    pausing / skipping from the Spotify app itself — left the web view
    showing a stale track. This polls in the background and re-pushes the
    card whenever the track or play/pause state changes on its own.

    The pushes are `refresh` frames: they update the card in place if it's
    on screen but never force it back up once dismissed, so the watcher
    stays in sync without hijacking the display. Change-detected, so steady
    playback of a single song is silent.

    Best-effort and self-contained: transient API/network errors back off;
    an unauthorised Spotify (no cached token) stops the watcher entirely —
    there's nothing to follow.
    """
    last_sig: tuple | None = None
    errors = 0
    empties = 0  # consecutive polls reporting nothing playing
    while True:
        try:
            cur = await asyncio.to_thread(zoro_spotify.now_playing)
            errors = 0
        except RuntimeError as e:
            # Not authorised / no token — nothing to watch, so stop cleanly.
            print(f"[zoro] spotify watch off: {e}", flush=True)
            return
        except Exception as e:  # noqa: BLE001 - transient API/network blip
            errors += 1
            if "403" in str(e) or errors >= 3:
                print(f"[zoro] spotify watch paused: {e}", flush=True)
                return
            print(f"[zoro] spotify watch error ({errors}): {e}", flush=True)
            await asyncio.sleep(min(_SPOTIFY_POLL_IDLE * errors, 60.0))
            continue

        empties = empties + 1 if cur is None else 0

        # Debounce the "nothing playing" state: a single empty reading is
        # usually just the gap between two tracks as the queue rolls over.
        # If we were playing a real track a moment ago, hold off on pushing
        # the empty card and re-check quickly, so a track change doesn't
        # flash "Nothing playing" before the next song registers.
        if (
            cur is None
            and empties < 2
            and last_sig is not None
            and last_sig[0] != "none"
        ):
            await asyncio.sleep(_SPOTIFY_POLL_NEAR_END)
            continue

        sig = _now_playing_sig(cur)
        if sig != last_sig:
            last_sig = sig
            await publish_display(
                "nowplaying", cur or {"empty": True}, refresh=True
            )

        if cur and cur.get("is_playing"):
            remaining = (cur.get("duration_ms") or 0) - (cur.get("progress_ms") or 0)
            delay = (
                _SPOTIFY_POLL_NEAR_END
                if 0 < remaining <= _SPOTIFY_NEAR_END_MS
                else _SPOTIFY_POLL_ACTIVE
            )
        elif cur is not None:
            # Paused but a track is still loaded — poll briskly so a resume
            # (from the app or elsewhere) shows up promptly.
            delay = _SPOTIFY_POLL_ACTIVE
        else:
            delay = _SPOTIFY_POLL_IDLE
        await asyncio.sleep(delay)


_NO_REFLECT_TOOLS = frozenset({
    "read_my_source",
    "read_my_notes",
    "what_time_is_it",
})


async def publish_tool_event(name: str, phase: str, ms: int | None = None) -> None:
    """Emit a live tool-invocation frame for the web HUD's activity ticker.

    phase is "active" (just started) | "ok" | "err" (finished). The web
    view shows the current tool and its outcome so the UI reflects what
    Zoro is actually doing in real time. Best-effort like all display."""
    data: dict = {"name": name, "phase": phase}
    if ms is not None:
        data["ms"] = ms
    await publish_display("tool", data)


# --- Tool Latency Concealment (Verbal fillers for slow / networked tools) ---
TOOL_FILLERS: dict[str, list[str]] = {
    "web_search": [
        "Looking that up, Sir...",
        "Searching the web for you...",
        "Let me check that on the web...",
    ],
    "describe_screen": [
        "Taking a look at your screen now...",
        "Looking at your display, Sir...",
    ],
    "check_calendar": [
        "Checking your schedule, Sir...",
        "Looking at your calendar...",
    ],
    "create_calendar_event": [
        "Adding that to your calendar, Sir...",
    ],
    "reschedule_calendar_event": [
        "Updating your calendar, Sir...",
    ],
    "cancel_calendar_event": [
        "Cancelling that calendar event...",
    ],
    "check_email": [
        "Checking your inbox, Sir...",
        "Looking for new messages...",
    ],
    "read_email": [
        "Opening that email for you...",
    ],
    "send_email": [
        "Sending that email now, Sir...",
    ],
    "send_whatsapp_message": [
        "Sending that on WhatsApp, Sir...",
    ],
    "read_whatsapp_messages": [
        "Checking your WhatsApp messages...",
    ],
    "play_music": [
        "Putting that on Spotify, Sir...",
        "Queueing that track up...",
    ],
    "play_my_playlist": [
        "Loading your playlist on Spotify...",
    ],
    "whats_playing": [
        "Checking what's playing...",
    ],
    "morning_briefing": [
        "Gathering your morning briefing, Sir...",
        "Pulling together your briefing now...",
    ],
    "read_document": [
        "Reading through that document now, Sir...",
    ],
    "ask_claude_code": [
        "Delegating that task to my Azure core now, Sir...",
    ],
    "delegate_task": [
        "Delegating that task to my Azure core now, Sir...",
    ],
    "where_am_i": [
        "Checking your location...",
    ],
    "find_files": [
        "Searching for those files, Sir...",
    ],
}

_last_filler_time: float = 0.0
_FILLER_COOLDOWN_SECONDS: float = 4.0


def _should_speak_filler() -> bool:
    global _last_filler_time
    now = time.monotonic()
    if now - _last_filler_time < _FILLER_COOLDOWN_SECONDS:
        return False
    _last_filler_time = now
    return True


def _get_tool_filler(tool_name: str) -> str | None:
    options = TOOL_FILLERS.get(tool_name)
    if not options:
        return None
    return random.choice(options)


async def _speak_filler(session, text: str) -> None:
    """Deliver an immediate low-latency spoken filler to mask tool execution delay.
    add_to_chat_ctx=False prevents polluting the conversation history."""
    try:
        await session.say(text, add_to_chat_ctx=False, allow_interruptions=True)
    except Exception:
        pass


def _logged(fn):
    """Wrap a function_tool method so every call writes a zoro_episodes row.

    Apply UNDER @function_tool so the LiveKit decorator still sees the raw
    signature (LiveKit reads inspect.signature, which follows __wrapped__).
    Telemetry is fire-and-forget on a worker thread — the voice loop never
    waits on Supabase.
    """
    tool_name = fn.__name__
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(self, context, *args, **kwargs):
        t0 = time.monotonic()
        outcome = "success"
        err: str | None = None
        result: object = None
        # Live HUD ticker: announce the tool firing before we run it.
        asyncio.create_task(publish_tool_event(tool_name, "active"))

        # Tool Latency Concealment: emit an immediate natural filler so the user never experiences dead silence
        session = getattr(context, "session", None)
        filler = _get_tool_filler(tool_name)
        if filler and session and _should_speak_filler():
            asyncio.create_task(_speak_filler(session, filler))

        try:
            result = await fn(self, context, *args, **kwargs)
            if isinstance(result, str) and zoro_episodes.looks_like_failure(result):
                outcome = "error"
            return result
        except BaseException as e:  # noqa: BLE001 - log then re-raise
            outcome = "error"
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            asyncio.create_task(publish_tool_event(
                tool_name, "err" if outcome == "error" else "ok", latency_ms
            ))
            try:
                bound = sig.bind_partial(self, context, *args, **kwargs)
                bound.apply_defaults()
                logged_args = {
                    k: v for k, v in bound.arguments.items()
                    if k not in ("self", "context")
                }
            except (TypeError, ValueError):
                logged_args = dict(kwargs)
            task_summary = " ".join(
                f"{k}={v!r}" for k, v in logged_args.items() if v not in (None, "", [])
            )[:300]
            result_str = result if isinstance(result, str) else (
                "" if result is None else repr(result)
            )
            # Capture locals the post-write needs (the wrapper coroutine
            # exits with the return immediately after this finally block).
            _outcome = outcome
            _err = err
            _result_str = result_str
            _logged_args = logged_args
            _task_summary = task_summary

            async def _persist() -> None:
                eid = await asyncio.to_thread(
                    zoro_episodes.log_episode,
                    kind="tool",
                    tool=tool_name,
                    task=_task_summary,
                    args=_logged_args,
                    outcome=_outcome,
                    latency_ms=latency_ms,
                    result_summary=_result_str,
                    error=_err,
                    session_id=_SESSION_ID,
                )
                # Only successful, non-noisy tool calls are candidates for
                # later correction-tagging — read-only and already-failed
                # episodes don't need to be flipped.
                if (
                    eid
                    and _outcome == "success"
                    and zoro_corrections.should_consider_for(tool_name)
                ):
                    _register_episode({
                        "id": eid,
                        "tool": tool_name,
                        "result": _result_str,
                        "ts": time.monotonic(),
                    })
                # Failed episodes get a reflection. The reflector calls
                # Claude Haiku + writes to zoro_lessons, both off the
                # voice loop. Don't reflect on reflection-internal tools
                # to avoid loops or noisy lessons.
                if (
                    eid
                    and _outcome == "error"
                    and tool_name not in _NO_REFLECT_TOOLS
                ):
                    await asyncio.to_thread(
                        zoro_lessons.reflect_on_episode,
                        episode_id=eid,
                        tool=tool_name,
                        task=_task_summary,
                        outcome=_outcome,
                        result_summary=_result_str,
                        error=_err,
                    )

            asyncio.create_task(_persist())

    return wrapper


def _similar_episodes_block() -> str:
    """Render the in-session self-improvement block for the system prompt.

    Unified Phase 3 lens — both reflection systems feed this block,
    preferred-quality first so they reinforce rather than contradict:

      1. The most recent unspoken daily report (Haiku-curated summary,
         the highest-quality signal — already deemed worth surfacing to
         Skhathi tomorrow morning).
      2. Top open lessons (Haiku-curated per-failure observations).
      3. Raw recent_failures rows (fallback — Phase 1 behaviour).

    All best-effort: any layer failing falls through to the next. Empty
    string if nothing is available."""
    daily = None
    try:
        daily = zoro_reflector.unspoken_report()
    except Exception:  # noqa: BLE001
        daily = None
    if daily and (daily.get("summary") or "").strip():
        return (
            "\n\n# Last night's self-improvement reflection (your own words)\n"
            + daily["summary"].strip()
            + "\n\nKeep this in mind; don't repeat yesterday's stumbles."
        )

    lessons: list[dict] = []
    try:
        lessons = zoro_lessons.open_lessons(limit=5)
    except Exception:  # noqa: BLE001
        lessons = []
    if lessons:
        lines = []
        for l in lessons:
            tool = l.get("tool", "?")
            text = (l.get("lesson") or "").strip()
            if not text:
                continue
            lines.append(f"- ({tool}) {text[:160]}")
        if lines:
            return (
                "\n\n# Open lessons (curated — apply these going forward)\n"
                + "\n".join(lines)
            )

    try:
        rows = zoro_episodes.recent_failures(5)
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    lines = []
    for r in rows:
        tool = r.get("tool", "?")
        when = (r.get("created_at") or "")[:10]
        what = (r.get("task") or r.get("result_summary") or r.get("error") or "").strip()
        if what:
            lines.append(f"- {when} {tool}: {what[:140]}")
    if not lines:
        return ""
    return (
        "\n\n# Recent tool failures (learn from these — don't repeat)\n"
        + "\n".join(lines)
    )


class Zoro(Agent):
    def __init__(self, instructions: str = ZORO_SYSTEM_PROMPT) -> None:
        super().__init__(instructions=instructions)

    @function_tool()
    async def remember(self, context: RunContext, fact: str) -> str:
        """Save a fact to long-term memory (persists across sessions and
        devices). Use when the user shares something worth remembering, or
        asks you to remember it.

        Args:
            fact: A concise statement of what to remember.
        """
        try:
            await asyncio.to_thread(zoro_memory.add_memory, fact)
        except Exception as e:  # noqa: BLE001
            return f"I couldn't save that to memory: {e}"
        return "I'll remember that."

    @function_tool()
    async def recall(self, context: RunContext, query: str = "") -> str:
        """Recall things from long-term memory.

        Args:
            query: What to look for. Leave empty for the most recent
                remembered facts.
        """
        try:
            if query.strip():
                items = await asyncio.to_thread(
                    zoro_memory.search_memories, query
                )
            else:
                items = await asyncio.to_thread(zoro_memory.recent_memories, 6)
        except Exception as e:  # noqa: BLE001
            return f"I couldn't reach memory: {e}"
        if not items:
            return "I don't have anything on that yet."
        return "Here's what I recall: " + "; ".join(items)

    @function_tool()
    async def stand_down(
        self, context: RunContext, resume: bool = False, minutes: int = 0
    ) -> str:
        """Suspend or resume Zoro's PROACTIVE interruptions only — this does
        NOT end the session. The voice call stays open and the user can
        keep talking to her; only spontaneous notifications are muted.

        Call with resume=False when the user explicitly asks for quiet:
        "be quiet", "don't interrupt me", "stop interrupting", "no
        notifications", "hush", "not now" (in the context of being
        bothered, not of ending the call). Call with resume=True for
        "resume", "you can talk again", "you can interrupt me again".

        IMPORTANT: Do NOT use this tool for "stand down", "go to sleep",
        "end session", "we're done" — those mean end the session entirely
        and must use `end_session` instead.

        Args:
            resume: True to resume proactivity, False to stand down.
            minutes: Optional auto-resume after this many minutes (only used
                when standing down; 0 means until explicitly resumed).
        """
        try:
            await asyncio.to_thread(
                zoro_safety.set_stand_down, not resume, minutes or None
            )
        except Exception as e:  # noqa: BLE001 - persistence can now fail loudly
            return f"I couldn't change that setting: {e}"
        if resume:
            return "Back online, Sir."
        if minutes:
            return f"Standing down for {minutes} minutes, Sir."
        return "Standing down, Sir. Say resume when you want me back."

    @function_tool()
    async def get_current_time(self, context: RunContext) -> str:
        """Return the user's current local date and time.

        Use this whenever the user asks what time or what day it is.
        """
        now = datetime.now()
        return now.strftime("It is %A, %B %d, %Y, at %I:%M %p.")

    @function_tool()
    @_logged
    async def open_app(self, context: RunContext, app_name: str) -> str:
        """Open an installed Windows application by name.

        Fuzzy-matches against the Start Menu and installed apps (Spotify,
        Discord, VS Code, Chrome, Calculator, Notepad, Settings, etc.). Spoken
        names and partial names are supported.

        Args:
            app_name: The app the user wants opened, e.g. "spotify", "notepad", "calculator".
        """
        key = app_name.strip()
        try:
            match = await asyncio.to_thread(zoro_shell.open_app, key)
            return f"Opening {match['name']}."
        except LookupError:
            pass
        except Exception:
            pass

        target = APP_ALIASES.get(key.lower(), key)
        try:
            subprocess.Popen(["cmd", "/c", "start", "", target])
            return f"Opening {app_name}."
        except OSError as e:
            return f"I couldn't open {app_name}: {e}"

    @function_tool()
    @_logged
    async def get_system_status(self, context: RunContext) -> str:
        """Check the computer's live hardware status and health.

        Returns battery level, charging status, time remaining, CPU usage,
        RAM utilization, and free disk space. Use whenever the user asks
        "how's my battery?", "am I charging?", "how much RAM do I have free?",
        "check system resources", "how's the PC doing?", or "is my disk full?".
        """
        try:
            res = await asyncio.to_thread(zoro_tools.get_system_telemetry)
        except Exception as e:
            return f"I couldn't read system telemetry: {e}"
        await publish_display("system", res)
        return res.get("summary", "System status retrieved.")

    @function_tool()
    @_logged
    async def list_running_apps(
        self, context: RunContext, limit: int = 10
    ) -> str:
        """List active user applications and processes sorted by memory usage.

        Use when the user asks "what's running?", "what apps are open?",
        or to identify process names before closing or terminating an app.

        Args:
            limit: Number of top processes to return (default 10, max 20).
        """
        try:
            clamped = min(max(limit, 3), 20)
            procs = await asyncio.to_thread(zoro_tools.list_running_processes, clamped)
        except Exception as e:
            return f"I couldn't list running processes: {e}"
        if not procs:
            return "No user processes found."
        names = [f"{p['name']} ({p['memory_mb']} MB)" for p in procs[:clamped]]
        return f"Top {len(names)} active processes: " + ", ".join(names)

    @function_tool()
    @_logged
    async def terminate_app(self, context: RunContext, name: str) -> str:
        """Close or terminate a running application or process by name or PID.

        STATE-CHANGING: can cause unsaved work to be lost. Confirm with the user
        before terminating an application if they did not explicitly command it.

        Args:
            name: Process name (e.g. 'notepad', 'spotify', 'chrome') or numeric PID.
        """
        try:
            res = await asyncio.to_thread(zoro_tools.terminate_process, name)
        except Exception as e:
            return f"Failed to terminate process: {e}"
        return res.get("message", "Done.")

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def web_search(self, context: RunContext, query: str) -> str:
        """Search the web for current or factual information.

        Use this for anything you don't know or that may have changed
        recently (news, prices, facts, people). Summarize the result
        out loud in one or two sentences — don't read it verbatim.

        Args:
            query: What to look up.
        """
        print(f"[zoro] web_search tool invoked: {query!r}", flush=True)
        try:
            result = await asyncio.to_thread(
                zoro_tools.search_web_detailed, query
            )
        except Exception as e:  # noqa: BLE001 - report any search failure aloud
            return f"My web search failed: {e}"
        # Show the full answer + sources on screen; speak only the summary.
        await publish_display("search", result)
        if result["answer"]:
            return result["answer"]
        if not result["results"]:
            return "No results found."
        return "\n".join(
            f"{r['title']}: {r['content']}" for r in result["results"]
        )

    @function_tool()
    async def control_audio(
        self, context: RunContext, action: str, steps: int = 4
    ) -> str:
        """Control system audio and media playback on the Windows PC.

        Args:
            action: One of "volume_up", "volume_down", "mute",
                "play_pause", "next_track", "prev_track".
            steps: How many volume increments (only for volume_up/down).
        """
        try:
            repeat = steps if action in ("volume_up", "volume_down") else 1
            zoro_tools.media_key(action, repeat)
        except ValueError as e:
            return str(e)
        return f"Done — {action.replace('_', ' ')}."

    @function_tool()
    async def lock_computer(self, context: RunContext) -> str:
        """Lock the Windows workstation immediately."""
        zoro_tools.lock_screen()
        return "Locking the computer."

    @function_tool()
    async def sleep_computer(self, context: RunContext) -> str:
        """Put the computer to sleep. Confirm with the user before using
        this — it is disruptive and ends the current session."""
        zoro_tools.sleep_pc()
        return "Going to sleep now."

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def describe_screen(self, context: RunContext, question: str) -> str:
        """Look at what's currently on the user's screen and answer a
        question about it. Use for "what's on my screen?", "what's this
        error?", "summarize this page", "what am I looking at?".

        This is read-only — just do it, no confirmation needed.

        Args:
            question: What the user wants to know about the screen.
        """
        try:
            res = await asyncio.to_thread(
                zoro_tools.describe_screen_detailed,
                question,
                os.environ.get("ANTHROPIC_API_KEY", ""),
            )
        except Exception as e:  # noqa: BLE001 - report failure aloud
            return f"I couldn't read the screen: {e}"
        await publish_display("vision", res)
        return res["answer"]

    @function_tool()
    async def show_chat_history(
        self, context: RunContext, show: bool = True
    ) -> str:
        """Show or hide the on-screen chat history / chat interface.

        The conversation is NOT displayed on screen while you talk — it is
        only surfaced when the user explicitly asks for it. Call with
        show=True when the user says things like "show me our chat history",
        "show me our chat interface", "show the transcript", "open the
        chat". Call with show=False when they say "hide the chat", "close
        the chat history", "hide the transcript".

        Args:
            show: True to reveal the chat history panel, False to hide it.
        """
        await publish_display("chat", {"show": show})
        return "Here's our chat history." if show else "Closed."

    @function_tool()
    async def close_widget(self, context: RunContext, target: str) -> str:
        """Close a widget (a content card) on screen, or close all of them.

        Call this when the user asks to dismiss something on screen, e.g.
        "close the weather widget", "close search", "close the calendar",
        "hide the inbox", "close all widgets", "clear the screen".

        Args:
            target: The widget kind to close. One of: "search", "calendar",
                "inbox", "weather", "briefing", "notes", "proactive",
                "vision", "chat", or "all" to close every open widget.
        """
        t = (target or "").strip().lower()
        # Map common aliases the LLM might pass through to canonical kinds.
        aliases = {
            "mail": "inbox", "email": "inbox", "emails": "inbox",
            "screen": "vision", "screenshot": "vision",
            "reminders": "notes", "note": "notes",
            "transcript": "chat", "history": "chat",
            "telemetry": "system", "diagnostics": "system",
            "web": "browser",
            "everything": "all", "clear": "all", "all widgets": "all",
        }
        t = aliases.get(t, t)
        valid = {
            "search", "calendar", "inbox", "weather", "briefing",
            "notes", "proactive", "vision", "chat", "system", "browser", "all",
        }
        if t not in valid:
            return f"I don't know a widget called {target!r}."
        await publish_display("close", {"target": t})
        if t == "all":
            return "Clearing the screen."
        return f"Closed the {t} widget."

    @function_tool()
    async def end_session(self, context: RunContext) -> str:
        """End the current voice session. Zoro stops listening and the
        call closes, but the wake service stays armed so the user can
        bring her back any time by saying "Hey Zoro".

        Call this whenever the user signals they want to wind the
        conversation down: "end session", "go to sleep", "stand down",
        "that's all for now", "that'll be all", "stop listening", "we're
        done", "dismissed", "goodbye" (when it clearly means wind down,
        not just acknowledging something).

        Do NOT use `stand_down` for these — that one only mutes proactive
        interruptions while keeping the call open.
        """
        await publish_display("session", {"action": "end"})
        # IMPORTANT: do NOT speak the wake phrase here — the TTS comes out
        # of the user's speakers, the mic picks it up, and the wake service
        # reactivates the session in a loop.
        return "Signing off, Sir."

    @function_tool()
    async def deactivate(self, context: RunContext) -> str:
        """Fully deactivate Zoro. The session ends AND the wake word is
        disarmed — "Hey Zoro" will be ignored until the user manually
        clicks Activate on the web interface.

        Call this only when the user explicitly says "deactivate",
        "shut down", "power down", or otherwise clearly asks for full
        deactivation (not just to end the current call).
        """
        try:
            await asyncio.to_thread(zoro_safety.set_stand_down, True, None)
        except Exception:
            pass
        try:
            from websockets.sync.client import connect as _ws_connect

            def _mute_local_wake() -> None:
                with _ws_connect("ws://127.0.0.1:8765", open_timeout=1.5) as ws:
                    ws.send('{"cmd": "mute"}')

            await asyncio.to_thread(_mute_local_wake)
        except Exception:
            pass
        await publish_display("session", {"action": "deactivate"})
        return "Deactivating. I'll need a manual activation to come back."

    @function_tool()
    @_logged
    async def type_text(self, context: RunContext, text: str) -> str:
        """Type text into whatever window currently has focus.

        This changes state — per your safety contract, only call this AFTER
        you have told the user what you'll type and they have confirmed.

        Args:
            text: The exact text to type.
        """
        await asyncio.to_thread(zoro_tools.type_text, text)
        return "Typed."

    @function_tool()
    @_logged
    async def press_keys(
        self, context: RunContext, keys: list[str]
    ) -> str:
        """Press a keyboard shortcut, e.g. ["ctrl","s"], ["enter"],
        ["alt","tab"], ["ctrl","w"].

        State-changing — confirm with the user first per your safety
        contract before pressing anything that could lose work.

        Args:
            keys: The keys to press together (single key = just press it).
        """
        try:
            await asyncio.to_thread(zoro_tools.press_keys, keys)
        except ValueError as e:
            return str(e)
        return f"Pressed {'+'.join(keys)}."

    @function_tool()
    @_logged
    async def click_screen(
        self, context: RunContext, x: int = -1, y: int = -1,
        button: str = "left",
    ) -> str:
        """Click the mouse. With no coordinates, clicks the current pointer
        position; otherwise at (x, y).

        State-changing — confirm with the user before clicking unless they
        just asked you to click something specific.

        Args:
            x: X pixel, or -1 for current position.
            y: Y pixel, or -1 for current position.
            button: "left", "right", or "middle".
        """
        cx = None if x < 0 else x
        cy = None if y < 0 else y
        await asyncio.to_thread(zoro_tools.mouse_click, cx, cy, button)
        return "Clicked."

    @function_tool()
    @_logged
    async def manage_window(self, context: RunContext, action: str) -> str:
        """Manage the focused window or desktop. Actions: "minimize",
        "maximize", "close", "show_desktop", "switch_window".

        "close" can lose unsaved work — confirm before using it per your
        safety contract.

        Args:
            action: One of the actions above.
        """
        try:
            await asyncio.to_thread(zoro_tools.window_action, action)
        except ValueError as e:
            return str(e)
        return f"Done — {action.replace('_', ' ')}."

    # --- Desktop control: prefer these fast deterministic tools ----------

    @function_tool()
    async def open_folder(self, context: RunContext, target: str) -> str:
        """Open a folder in File Explorer. `target` can be a known name
        (downloads, documents, desktop, pictures, music, videos, home) or
        any path. Read-only — just do it.

        Args:
            target: Folder name or path, e.g. "downloads" or "C:/Tools".
        """
        try:
            p = await asyncio.to_thread(zoro_tools.open_folder, target)
        except (FileNotFoundError, OSError) as e:
            return f"I couldn't open that folder: {e}"
        return f"Opened {p}."

    @function_tool()
    async def list_folder(self, context: RunContext, target: str) -> str:
        """List what's inside a folder. Read-only — just do it.

        Args:
            target: Folder name or path (e.g. "downloads").
        """
        try:
            items = await asyncio.to_thread(zoro_tools.list_folder, target)
        except (FileNotFoundError, NotADirectoryError, OSError) as e:
            return f"I couldn't read that folder: {e}"
        if not items:
            return "That folder is empty."
        return f"{len(items)} items: " + ", ".join(items[:25])

    @function_tool()
    async def find_files(
        self, context: RunContext, query: str, folder: str = ""
    ) -> str:
        """Search for files by name. Searches Downloads, Documents and
        Desktop unless a folder is given. Read-only — just do it.

        Args:
            query: Part of the file name to look for.
            folder: Optional folder to search within.
        """
        try:
            hits = await asyncio.to_thread(
                zoro_tools.find_files, query, folder or None
            )
        except OSError as e:
            return f"Search failed: {e}"
        if not hits:
            return f"I found no files matching '{query}'."
        names = [zoro_tools.Path(h).name for h in hits[:6]]
        return f"Found {len(hits)}: " + "; ".join(names)

    @function_tool()
    @_logged
    async def open_item(
        self, context: RunContext, target: str, folder: str = ""
    ) -> str:
        """Open a file or folder with its default app. If `target` is an
        existing path it opens directly; otherwise it's treated as a name
        to search for (Downloads/Documents/Desktop, or `folder`) and the
        best match is opened. Use this for "open my X document".

        Args:
            target: A path, or part of a file name to find and open.
            folder: Optional folder to search within.
        """
        try:
            from pathlib import Path as _P

            if _P(target).expanduser().exists():
                p = await asyncio.to_thread(zoro_tools.open_path, target)
            else:
                p = await asyncio.to_thread(
                    zoro_tools.find_and_open, target, folder or None
                )
        except (FileNotFoundError, OSError) as e:
            return f"I couldn't open that: {e}"
        return f"Opened {zoro_tools.Path(p).name}."

    @function_tool()
    @_logged
    async def file_op(
        self, context: RunContext, action: str, source: str,
        destination: str = "",
    ) -> str:
        """Copy, move, or rename a file. State-changing — only call after
        the user has confirmed the task.

        Args:
            action: "copy", "move", or "rename".
            source: The file to act on (path).
            destination: Target path/folder, or new name for "rename".
        """
        try:
            if action == "copy":
                d = await asyncio.to_thread(
                    zoro_tools.copy_file, source, destination
                )
            elif action == "move":
                d = await asyncio.to_thread(
                    zoro_tools.move_file, source, destination
                )
            elif action == "rename":
                d = await asyncio.to_thread(
                    zoro_tools.rename_file, source, destination
                )
            else:
                return 'Action must be "copy", "move", or "rename".'
        except (OSError, ValueError) as e:
            return f"That file operation failed: {e}"
        return f"Done — {action} to {zoro_tools.Path(d).name}."

    @function_tool()
    async def read_document(self, context: RunContext, path: str) -> str:
        """Read a text-like file's contents (txt, md, csv, code, json…).
        Binary docs (.pdf/.docx) can be opened with open_item but not read
        aloud yet. Read-only — just do it.

        Args:
            path: The file to read (path, or use find_files first).
        """
        try:
            return await asyncio.to_thread(zoro_tools.read_text_file, path)
        except (FileNotFoundError, ValueError, OSError) as e:
            return str(e)

    @function_tool()
    async def clipboard(
        self, context: RunContext, mode: str, text: str = ""
    ) -> str:
        """Read or set the Windows clipboard.

        Args:
            mode: "get" to read it, "set" to put `text` on it.
            text: The text to copy (only for mode="set").
        """
        try:
            if mode == "set":
                await asyncio.to_thread(zoro_tools.clipboard_set, text)
                return "Copied to clipboard."
            val = await asyncio.to_thread(zoro_tools.clipboard_get)
        except OSError as e:
            return f"I couldn't access the clipboard: {e}"
        return f"Clipboard: {val}" if val else "The clipboard is empty."

    @function_tool()
    async def control_computer(
        self, context: RunContext, task: str
    ) -> str:
        # NOTE: deliberately NOT wrapped in @_logged — zoro_computer.run_task
        # already writes a richer kind='computer_use' episode (steps,
        # latency, classified outcome). Double-decorating produced paired
        # rows with the outer one misclassified as 'success' because the
        # "I stopped…" return string doesn't match the apology heuristic.
        """Visually control the computer to do a multi-step task in an app's
        UI — only when no other tool fits (open_folder/open_item/find_files
        handle files & folders far faster, prefer those). This watches the
        screen and clicks/types step by step, so it's slow; confirm the
        whole task with the user before calling it, then it runs to
        completion. Respects "stand down".

        Args:
            task: Plain-language goal, e.g. "in Spotify, search for and
                play my Focus playlist".
        """
        try:
            return await asyncio.to_thread(
                zoro_computer.run_task, task, os.environ.get("ANTHROPIC_API_KEY", "")
            )
        except Exception as e:  # noqa: BLE001 - report aloud
            return f"My computer control failed: {e}"

    # --- Zoro's Dedicated Browser Automation (Playwright) -------------------

    @function_tool()
    @_logged
    async def browser_navigate(self, context: RunContext, url: str) -> str:
        """Navigate Zoro's dedicated background browser to a website or localhost URL.

        Unlike opening the desktop browser, this operates silently in Zoro's
        isolated virtual browser without stealing focus or moving the user's cursor.
        Streams the live viewport directly to the web HUD.

        Use for navigating documentation, checking localhost dev servers
        (e.g. 'localhost:3000'), testing web apps, or deep web research.

        Args:
            url: The web address or localhost endpoint to open (e.g. 'localhost:3000', 'github.com/trending').
        """
        browser = zoro_browser.get_browser()
        res = await browser.navigate(url)
        if not res.get("ok"):
            return res.get("error", "Failed to navigate.")
        await publish_display("browser", res)
        msg = f"Opened {res.get('title')} at {res.get('url')}."
        if res.get("auth_required"):
            msg += " Notice: An authentication screen, login form, or security check was detected on this page. If you need to sign in or solve 2FA, call browser_handoff so the user can enter credentials."
        return msg

    @function_tool()
    @_logged
    async def browser_click(self, context: RunContext, target: str) -> str:
        """Click a button, link, or element inside Zoro's dedicated browser.

        Args:
            target: The button text, link text, or CSS selector to click (e.g. 'Sign in', 'Get Started', '#submit-btn').
        """
        browser = zoro_browser.get_browser()
        res = await browser.click(target)
        if not res.get("ok"):
            return res.get("error", "Could not click element.")
        await publish_display("browser", res)
        msg = f"Clicked '{target}'. Current page: {res.get('title')}."
        if res.get("auth_required"):
            msg += " Notice: An authentication screen or login form was detected. If login or 2FA is needed, invoke browser_handoff."
        return msg

    @function_tool()
    @_logged
    async def browser_handoff(
        self, context: RunContext, reason: str = "Please log in or authenticate"
    ) -> str:
        """Switch Zoro's browser to a visible desktop window so the user can log in or solve 2FA/CAPTCHA.

        Call this when an authentication gate, CAPTCHA, or sign-in page is reached, or when the
        user asks to take over Zoro's browser window to input sensitive credentials.
        The browser window pops up directly on the user's desktop at the current page.
        When finished, the user can say "Zoro, I'm logged in" or click 'Hand Back to Zoro'.

        Args:
            reason: The reason for the handoff (e.g. 'Sign in to GitHub', 'Solve Cloudflare CAPTCHA').
        """
        browser = zoro_browser.get_browser()
        res = await browser.handoff_to_user(reason)
        if not res.get("ok"):
            return res.get("error", "Could not open browser window on desktop.")
        await publish_display("browser", res)
        return (
            f"I have opened the browser on your desktop so you can {reason}. "
            "Whenever you are ready, just let me know or click 'Hand back to Zoro', and I will resume background control."
        )

    @function_tool()
    @_logged
    async def browser_resume(self, context: RunContext) -> str:
        """Resume autonomous background browsing after the user has finished logging in or authenticating.

        Closes the visible desktop browser window while preserving all session cookies, tokens, and storage,
        and returns Zoro to autonomous background control.
        """
        browser = zoro_browser.get_browser()
        res = await browser.resume_from_user()
        if not res.get("ok"):
            return res.get("error", "Could not resume autonomous browser control.")
        await publish_display("browser", res)
        return (
            f"Thank you, Sir! I have resumed autonomous control at {res.get('title')} ({res.get('url')}). "
            "All session credentials and cookies are safely saved. What would you like me to do next?"
        )

    @function_tool()
    @_logged
    async def browser_scroll(
        self, context: RunContext, direction: str = "down", amount: int = 500
    ) -> str:
        """Scroll the page inside Zoro's dedicated browser.

        Args:
            direction: 'down' or 'up'.
            amount: Scroll distance in pixels (default 500).
        """
        browser = zoro_browser.get_browser()
        res = await browser.scroll(direction, amount)
        if not res.get("ok"):
            return res.get("error", "Could not scroll.")
        await publish_display("browser", res)
        return f"Scrolled {direction}."

    @function_tool()
    @_logged
    async def browser_read(
        self, context: RunContext, max_chars: int = 3000
    ) -> str:
        """Read and extract clean text content from Zoro's active browser page.

        Use to read documentation, inspect web app state, or summarize
        articles currently loaded in Zoro's browser.

        Args:
            max_chars: Maximum characters to read (default 3000).
        """
        browser = zoro_browser.get_browser()
        res = await browser.read_content(max_chars)
        if not res.get("ok"):
            return res.get("error", "Could not read page content.")
        content = res.get("content", "").strip()
        if not content:
            return "The active page appears to be empty."
        return f"Page: {res.get('title')}\n\n{content}"

    @function_tool()
    @_logged
    async def browser_type(
        self, context: RunContext, selector: str, text: str
    ) -> str:
        """Type text into an input field or search bar in Zoro's dedicated browser.

        Args:
            selector: Input field CSS selector or placeholder name (e.g. 'input[name=q]', '#search', 'input[type=email]').
            text: Text to enter into the field.
        """
        browser = zoro_browser.get_browser()
        res = await browser.type_text(selector, text)
        if not res.get("ok"):
            return res.get("error", "Could not type into field.")
        await publish_display("browser", res)
        return f"Typed text into {selector}."

    @function_tool()
    @_logged
    async def browser_close(self, context: RunContext) -> str:
        """Close Zoro's dedicated browser session and dismiss the browser HUD card."""
        browser = zoro_browser.get_browser()
        await browser.close()
        await publish_display("close", {"target": "browser"})
        return "Closed browser session."

    @function_tool()
    @_logged
    async def search_youtube(
        self, context: RunContext, query: str, limit: int = 5
    ) -> str:
        """Search YouTube and show the top results on the HUD.

        Read-only — does NOT play anything. Use for "search YouTube for
        X", "find me a video about Y". For "play X on YouTube", use
        play_on_youtube instead.

        Args:
            query: What to search for.
            limit: How many results (default 5, max 10).
        """
        try:
            results = await asyncio.to_thread(
                zoro_youtube.search, query, min(max(limit, 1), 10)
            )
        except Exception as e:  # noqa: BLE001
            return _youtube_error(e)
        await publish_display(
            "youtube",
            {"mode": "results", "query": query, "results": results},
        )
        if not results:
            return f"No YouTube hits for {query!r}."
        top = results[0]
        return (
            f"Top result for {query!r}: {top['title']} by "
            f"{top['channel']}."
        )

    @function_tool()
    @_logged
    async def play_on_youtube(
        self, context: RunContext, query: str
    ) -> str:
        """Find a YouTube video and open the top result in the user's
        default browser. Use for "play X on YouTube".

        Args:
            query: What to play.
        """
        try:
            top = await asyncio.to_thread(
                zoro_youtube.search_and_open, query
            )
        except Exception as e:  # noqa: BLE001
            return _youtube_error(e)
        if not top:
            return f"No YouTube hits for {query!r}."
        await publish_display(
            "youtube",
            {"mode": "results", "query": query, "results": [top]},
        )
        return f"Opening {top['title']} by {top['channel']}."

    @function_tool()
    @_logged
    async def youtube_transcript(
        self, context: RunContext, query_or_url: str
    ) -> str:
        """Fetch the transcript of a YouTube video.

        `query_or_url` may be a video URL, an 11-char video id, or a
        search query (top hit is used). Speaks a short confirmation;
        full transcript lands on the card.

        Args:
            query_or_url: URL, video id, or search query.
        """
        try:
            data = await asyncio.to_thread(
                zoro_youtube.transcript, query_or_url
            )
        except LookupError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return _youtube_error(e)
        await publish_display(
            "youtube",
            {
                "mode": "transcript",
                "video": data["video"],
                "text": data["text"],
            },
        )
        title = (data.get("video") or {}).get("title", "the video")
        words = len((data["text"] or "").split())
        return f"Transcript of {title} is on screen — about {words} words."

    @function_tool()
    @_logged
    async def summarise_youtube(
        self, context: RunContext, query_or_url: str
    ) -> str:
        """Fetch a YouTube transcript and have Claude summarise it.

        Same input flexibility as youtube_transcript. The spoken reply
        is the full summary (one paragraph), and the card shows it
        alongside the source video.

        Args:
            query_or_url: URL, video id, or search query.
        """
        try:
            data = await asyncio.to_thread(
                zoro_youtube.transcript, query_or_url
            )
        except LookupError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return _youtube_error(e)
        try:
            summary = await asyncio.to_thread(
                zoro_youtube.summarise, data["text"]
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't summarise that: {e}"
        await publish_display(
            "youtube",
            {
                "mode": "summary",
                "video": data["video"],
                "summary": summary,
            },
        )
        return summary

    @function_tool()
    @_logged
    async def check_whatsapp(
        self, context: RunContext, limit: int = 8
    ) -> str:
        """Read the user's unread WhatsApp chats.

        Use for "any WhatsApp messages", "any new chats", "what did I
        miss on WhatsApp". Read-only — just do it.

        Args:
            limit: How many unread chats to summarise (default 8).
        """
        try:
            chats = await asyncio.to_thread(
                zoro_whatsapp.unread_chats, limit
            )
        except zoro_whatsapp.WhatsAppNotReady as e:
            return (
                "WhatsApp isn't connected yet — the bridge says "
                f"\"{e}\". Run the QR scan once."
            )
        except Exception as e:  # noqa: BLE001
            return _whatsapp_error(e)
        await publish_display(
            "whatsapp",
            {"mode": "unread", "chats": chats},
        )
        if not chats:
            return "No unread WhatsApp messages."
        total = sum(c.get("unread_count", 0) for c in chats)
        first = chats[0]
        return (
            f"{total} unread across {len(chats)} chat"
            f"{'s' if len(chats) != 1 else ''}. "
            f"Newest from {first['name']}: {first['last_body'][:120]}"
        )

    @function_tool()
    @_logged
    async def send_whatsapp(
        self, context: RunContext, to: str, text: str
    ) -> str:
        """Send a WhatsApp message.

        State-changing: per your safety contract, only call this AFTER
        you have read back the recipient and the full message body, and
        the user has confirmed.

        Args:
            to: Recipient. Either a chat id (e.g. "27821234567@c.us")
                or a name (substring, case-insensitive) that matches a
                chat you've talked to before.
            text: The message body.
        """
        try:
            await asyncio.to_thread(
                zoro_whatsapp.send_message, to, text
            )
        except LookupError:
            return f"I couldn't find a WhatsApp chat matching {to!r}."
        except zoro_whatsapp.WhatsAppNotReady as e:
            return f"WhatsApp isn't connected: {e}"
        except Exception as e:  # noqa: BLE001
            return _whatsapp_error(e)
        return f"Sent to {to}."

    @function_tool()
    @_logged
    async def reply_to_last_whatsapp(
        self, context: RunContext, text: str
    ) -> str:
        """Reply to whoever last messaged on WhatsApp.

        State-changing: read back the recipient ("the last message was
        from X — replying with Y") and confirm before calling.

        Args:
            text: The reply body.
        """
        try:
            res = await asyncio.to_thread(
                zoro_whatsapp.reply_to_last, text
            )
        except LookupError:
            return "There's no recent incoming WhatsApp to reply to."
        except zoro_whatsapp.WhatsAppNotReady as e:
            return f"WhatsApp isn't connected: {e}"
        except Exception as e:  # noqa: BLE001
            return _whatsapp_error(e)
        return f"Replied to {res.get('chat_id', '')}."

    @function_tool()
    @_logged
    async def call_whatsapp(
        self, context: RunContext, to: str, video: bool = False
    ) -> str:
        """Start a WhatsApp voice or video call with a contact.

        State-changing: per your safety contract, read back who you're
        calling and whether it's voice or video, and get the user's
        confirmation BEFORE calling this.

        How it works: this first tries to ring the contact directly. If
        the running WhatsApp Web build won't let us ring them, it falls
        back to generating a joinable call link and sending it to them as
        a message — so a call always gets started one way or the other.

        Args:
            to: Who to call. Either a chat id (e.g. "27821234567@c.us")
                or a name (substring, case-insensitive) that matches a
                chat you've talked to before. Must be a person, not a
                group.
            video: True for a video call, False (default) for voice.
        """
        kind = "video" if video else "voice"
        try:
            res = await asyncio.to_thread(
                zoro_whatsapp.place_call, to, video=video, method="auto"
            )
        except LookupError:
            return f"I couldn't find a WhatsApp chat matching {to!r}."
        except zoro_whatsapp.WhatsAppNotReady as e:
            return f"WhatsApp isn't connected: {e}"
        except Exception as e:  # noqa: BLE001
            return _whatsapp_error(e)
        if res.get("method") == "link":
            return (
                f"I couldn't ring {to} directly, so I sent them a {kind} "
                "call link to tap and join."
            )
        return f"Started a {kind} call with {to}."

    @function_tool()
    @_logged
    async def run_powershell(
        self, context: RunContext, command: str
    ) -> str:
        """Run a PowerShell command on the user's Windows machine.

        UNRESTRICTED SHELL ACCESS. Per your safety contract you MUST
        read the exact command back to the user verbatim and get an
        explicit "yes" before calling this. For destructive operations
        (`Remove-Item -Recurse`, `Format-Volume`, `Stop-Computer`,
        anything that uninstalls or modifies the registry broadly)
        require a second confirmation and warn what will be lost.
        Respects "stand down".

        Use for: installing apps (`winget install <id>`), uninstalling,
        system tweaks, scripted automation the user explicitly asks
        for. Do NOT use for things that already have dedicated tools
        (file ops → open_folder/find_files, app launching →
        open_installed_app, Spotify, GitHub, etc.). Every command run
        through this tool is appended to data/shell_audit.log.

        Args:
            command: The PowerShell command to execute, verbatim.
                Multi-line is OK; use `;` to chain.
        """
        try:
            res = await asyncio.to_thread(
                zoro_shell.run_powershell, command, 240
            )
        except subprocess.TimeoutExpired:
            return "That command ran past the four-minute timeout."
        except Exception as e:  # noqa: BLE001
            return f"PowerShell error: {e}"
        await publish_display(
            "shell",
            {
                "command": command,
                "exit_code": res["exit_code"],
                "stdout": res["stdout"],
                "stderr": res["stderr"],
            },
        )
        if res["exit_code"] == 0:
            out = (res["stdout"] or "").strip()
            if not out:
                return "Done."
            first = out.splitlines()[0][:140]
            return f"Done. {first}"
        err = (res["stderr"] or res["stdout"] or "").strip()
        first = err.splitlines()[0][:140] if err else ""
        return (
            f"Command failed (exit {res['exit_code']}). {first}"
            if first
            else f"Command failed with exit code {res['exit_code']}."
        )

    @function_tool()
    @_logged
    async def open_installed_app(
        self, context: RunContext, name: str
    ) -> str:
        """Alias for open_app: open an installed Windows app by name.

        Args:
            name: App name. Partial / fuzzy match is fine.
        """
        return await self.open_app(context, app_name=name)  # pyright: ignore[reportCallIssue]

    @function_tool()
    @_logged
    async def github_notifications(
        self, context: RunContext, limit: int = 10
    ) -> str:
        """Read the user's unread GitHub notifications across all repos.

        Use for "any GitHub notifications", "what's happening on
        GitHub", "GitHub inbox". Read-only — just do it.

        Args:
            limit: Max notifications to fetch (default 10).
        """
        try:
            items = await asyncio.to_thread(
                zoro_github.notifications, limit
            )
        except Exception as e:  # noqa: BLE001
            return _gh_error(e)
        unread = [n for n in items if n.get("unread")]
        await publish_display(
            "github",
            {"mode": "notifications", "items": items},
        )
        if not items:
            return "GitHub's quiet — no notifications."
        if not unread:
            return f"{len(items)} notifications, all read."
        # Spoken: just count + the first one's repo so it's not a list.
        first = unread[0]
        return (
            f"{len(unread)} unread GitHub notification"
            f"{'s' if len(unread) != 1 else ''}. "
            f"Newest is on {first['repo']}: {first['title']}."
        )

    @function_tool()
    @_logged
    async def github_my_prs(
        self, context: RunContext, limit: int = 20
    ) -> str:
        """List the user's open pull requests across all repos.

        Use for "what are my PRs", "any open PRs", "show my pull
        requests". Read-only.

        Args:
            limit: Max PRs to fetch (default 20).
        """
        try:
            items = await asyncio.to_thread(zoro_github.my_open_prs, limit)
        except Exception as e:  # noqa: BLE001
            return _gh_error(e)
        await publish_display(
            "github",
            {"mode": "prs", "items": items},
        )
        if not items:
            return "You have no open pull requests."
        draft = sum(1 for p in items if p.get("draft"))
        ready = len(items) - draft
        if draft:
            return (
                f"You have {len(items)} open PRs — {ready} ready, "
                f"{draft} draft."
            )
        return f"You have {len(items)} open PR{'s' if len(items) != 1 else ''}."

    @function_tool()
    @_logged
    async def github_pr_status(
        self, context: RunContext, repo: str, number: int
    ) -> str:
        """Get the full status of a specific pull request: checks,
        reviews, mergeable state.

        Args:
            repo: "owner/name", e.g. "anthropics/claude-code".
            number: PR number.
        """
        try:
            pr = await asyncio.to_thread(zoro_github.pr_details, repo, number)
        except Exception as e:  # noqa: BLE001
            return _gh_error(e)
        await publish_display(
            "github",
            {"mode": "pr", "pr": pr},
        )
        # One-line spoken summary.
        bits = [f"PR {pr['number']} on {repo} — {pr['title']}"]
        bits.append(f"state {pr['state'].lower()}")
        if pr.get("draft"):
            bits.append("draft")
        rd = pr.get("review_decision") or "PENDING"
        if rd and rd != "PENDING":
            bits.append(f"reviews {rd.lower().replace('_', ' ')}")
        counts = pr.get("checks_counts") or {}
        if counts:
            parts = [f"{v} {k.lower()}" for k, v in counts.items()]
            bits.append("checks " + ", ".join(parts))
        return "; ".join(bits) + "."

    @function_tool()
    @_logged
    async def github_open_pr(
        self,
        context: RunContext,
        repo: str,
        head_branch: str,
        title: str,
        body: str = "",
        base_branch: str = "main",
    ) -> str:
        """Open a new pull request on GitHub.

        State-changing: per your safety contract, read back repo, branch,
        title, and gist of the body, and ONLY call this after the user
        has confirmed. The head branch must already be pushed.

        Args:
            repo: "owner/name".
            head_branch: The branch you want merged.
            title: PR title.
            body: PR description (plain text).
            base_branch: Branch to merge into. Default "main".
        """
        try:
            url = await asyncio.to_thread(
                zoro_github.open_pr,
                repo,
                head_branch,
                base_branch,
                title,
                body,
            )
        except Exception as e:  # noqa: BLE001
            return _gh_error(e)
        await publish_display(
            "github",
            {"mode": "created", "pr": {"url": url, "title": title}},
        )
        return f"Opened the PR — {url}"

    @function_tool()
    @_logged
    async def self_improvement_report(
        self, context: RunContext, force_regenerate: bool = False
    ) -> str:
        """Return today's self-improvement report — what you learned
        and want to do better.

        Use this when Skhathi asks any of: "what have you learned today",
        "what would you like to improve", "what do you need", "did you
        improve yourself today". Speak the summary back in your own
        voice naturally; do not preface with "the report says".

        Args:
            force_regenerate: If true, run the nightly reflection now
                instead of returning the cached report. Use only when
                Skhathi explicitly asks for a fresh reflection.
        """
        if force_regenerate:
            out = await asyncio.to_thread(zoro_reflector.run_once)
            status = out.get("status", "")
            if status == "idle":
                return "I haven't logged anything reflective today yet."
            if status != "wrote":
                return "I couldn't produce a fresh report just now."
        rpt = await asyncio.to_thread(zoro_reflector.unspoken_report)
        if rpt is None:
            # Fall back to most recent any report
            base = zoro_episodes._base()
            if base is None:
                return "I don't have a reflection available right now."
            url, headers = base
            try:
                r = await asyncio.to_thread(
                    lambda: httpx.get(
                        f"{url}/rest/v1/zoro_daily_reports"
                        f"?order=report_date.desc&limit=1",
                        headers=headers, timeout=8,
                    )
                )
                r.raise_for_status()
                rows = r.json() or []
                rpt = rows[0] if rows else None
            except Exception:  # noqa: BLE001
                rpt = None
        if not rpt or not rpt.get("summary"):
            return "I haven't put together a reflection yet today."
        return rpt["summary"].strip()

    @function_tool()
    @_logged
    async def read_my_source(
        self, context: RunContext, name: str
    ) -> str:
        """Read one of your own source files so you can discuss or reason
        about your code before deciding to change it.

        Use this when the user asks about how you work, what a module
        does, or before you spawn Claude Code so you can name the
        exact file and the change you want. Do NOT read the source aloud
        — summarise in a sentence or two.

        Args:
            name: A bare module name (e.g. "zoro_proactive"), filename
                ("zoro_proactive.py"), or repo-relative path
                ("src/zoro_proactive.py"). Search is confined to src/
                and scripts/.
        """
        return await asyncio.to_thread(zoro_tools.read_self_source, name)

    @function_tool()
    @_logged
    async def ask_claude_code(
        self, context: RunContext, task: str, role: str = ""
    ) -> str:
        """Hand an engineering, coding, or editing task off to Zoro's Azure OpenAI Autonomous Engineer.

        Fire-and-forget: runs in the repository on a fresh branch off main.
        After it finishes, a boot check runs and the branch is auto-merged
        to main if it passes, or rolled back if it fails. Touching protected
        files is always rolled back. Progress streams to the web card.

        Use for things Zoro herself can't do: writing/refactoring code,
        adding features, fixing bugs in the repo.

        Args:
            task: Plain-language task, e.g. "add a dark-mode toggle to
                the web app" or "fix the import error in zoro_proactive".
            role: Optional sub-agent role name (e.g. "researcher",
                "coder", "agent_smith").
        """
        asyncio.create_task(_run_claude_task(
            task, context.session, role=(role.strip() or None),
        ))
        suffix = f" using the {role.strip()} role" if role.strip() else ""
        return (
            f"I'm on it Sir — delegating to my Azure engineering core now{suffix}. "
            "Watch the screen for progress."
        )

    @function_tool()
    @_logged
    async def delegate_task(
        self, context: RunContext, task: str, role: str = ""
    ) -> str:
        """Hand an engineering, coding, or editing task off to Zoro's Azure OpenAI Autonomous Engineer."""
        return await self.ask_claude_code(context, task, role)  # pyright: ignore[reportArgumentType]

    @function_tool()
    @_logged
    async def list_sub_agents(self, context: RunContext) -> str:
        """List your available sub-agent roles and what each one does.

        Use this when Skhathi asks "what sub-agents do you have" or
        before you decide which role to pass to ask_claude_code.
        """
        import zoro_subagents  # pyright: ignore[reportMissingImports]
        agents = await asyncio.to_thread(zoro_subagents.list_agents)
        if not agents:
            return "I have no sub-agents yet."
        lines = [f"{a.name}: {a.description}" for a in agents]
        return " | ".join(lines)

    @function_tool()
    @_logged
    async def build_sub_agent(
        self, context: RunContext, name: str, description: str
    ) -> str:
        """Spawn the agent_smith sub-agent to author a new role file
        under `agents/`.

        Use this when you've identified a recurring task that would
        benefit from a dedicated specialist (e.g. a writer, a
        diagnostician, a summariser).

        Args:
            name: Lower_snake_case identifier, single word preferred
                (e.g. "diagnostician").
            description: One-line purpose for the new role; Agent Smith
                will turn this into a full prompt.
        """
        clean = (name or "").strip().lower().replace(" ", "_")
        if not clean.replace("_", "").isalnum() or not clean:
            return "That name isn't valid — use lower_snake_case letters and digits only."
        task = (
            f"Create a new sub-agent named '{clean}'. Purpose: "
            f"{description.strip()}. Follow the format of the existing "
            f"agents/researcher.md and agents/coder.md exactly. Write "
            f"only the file agents/{clean}.md."
        )
        asyncio.create_task(_run_claude_task(
            task, context.session, role="agent_smith",
        ))
        return (
            f"I'll have Agent Smith draft the new {clean} sub-agent. "
            "Watch the screen for progress."
        )

    @function_tool()
    async def take_note(self, context: RunContext, note: str) -> str:
        """Save a short note for the user to a local notes file.

        Args:
            note: The note text to remember.
        """
        await asyncio.to_thread(zoro_tools.append_note, note)
        await publish_notes_panel()
        return "Noted."

    @function_tool()
    async def read_my_notes(self, context: RunContext, limit: int = 5) -> str:
        """Read back the user's most recent saved notes.

        Args:
            limit: How many recent notes to read (default 5).
        """
        notes = await asyncio.to_thread(zoro_tools.read_notes, limit)
        await publish_notes_panel()
        if not notes:
            return "You have no saved notes."
        return "Your recent notes: " + "; ".join(notes)

    @function_tool()
    async def set_reminder(
        self, context: RunContext, seconds: int, message: str
    ) -> str:
        """Set a timer/reminder that Zoro speaks aloud when it elapses.

        Convert the user's phrasing to seconds yourself (e.g. "5 minutes"
        -> 300). The reminder is saved durably and fires via the proactive
        loop even if this session has ended by then.

        Args:
            seconds: Delay before the reminder fires.
            message: What to remind the user about.
        """
        await asyncio.to_thread(zoro_tools.add_reminder, seconds, message)
        await publish_notes_panel()
        mins = seconds // 60
        when = f"{mins} minute{'s' if mins != 1 else ''}" if mins else f"{seconds} seconds"
        return f"Okay, I'll remind you in {when}."

    # --- Task queue (do-something-later, not just speak-something-later) ---

    @function_tool()
    @_logged
    async def schedule_task(
        self, context: RunContext, seconds: int, title: str,
        detail: str = "",
    ) -> str:
        """Schedule a TASK to carry out later — not just a thing to say. Use
        this when the user asks you to *do* something at a later time:
        "tonight, send that email to John", "in an hour, summarise this
        document for me", "tomorrow morning, check the news on the merger".

        For a plain "remind me to…" (just speak a string at time T), use
        `set_reminder` instead. Convert the user's phrasing to seconds
        yourself (e.g. "tonight at 8" -> compute the delta).

        Args:
            seconds: Delay until the task should fire.
            title: Short summary spoken back at fire time (one phrase).
            detail: Anything you'll need at fire time to actually do it —
                recipient, subject draft, the specific URL, etc.
        """
        from datetime import timedelta
        due = datetime.now() + timedelta(seconds=max(0, int(seconds)))
        tid = await asyncio.to_thread(zoro_tasks.add_task, due, title, detail or None)
        if not tid:
            return "I couldn't save that task."
        mins = seconds // 60
        when = (
            f"{mins} minute{'s' if mins != 1 else ''}"
            if mins < 60 else f"{mins // 60} hour{'s' if mins // 60 != 1 else ''}"
        )
        return f"Scheduled — I'll pick that up in {when}."

    @function_tool()
    @_logged
    async def list_my_tasks(self, context: RunContext) -> str:
        """List the user's currently-scheduled tasks (pending only). Use for
        "what tasks do I have?", "what are you supposed to do later?"."""
        tasks = await asyncio.to_thread(zoro_tasks.list_tasks, "pending", 10)
        if not tasks:
            return "You have no scheduled tasks."
        lines = []
        for t in tasks:
            # Supabase returns due_at as ISO UTC; show local time.
            try:
                due_utc = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00"))
                local = due_utc.astimezone()
                when = local.strftime("%a %I:%M %p")
            except (KeyError, ValueError):
                when = "(unknown)"
            lines.append(f"{t['title']} at {when}")
        return f"You have {len(tasks)} task{'s' if len(tasks) != 1 else ''}: " + "; ".join(lines)

    @function_tool()
    @_logged
    async def cancel_task(self, context: RunContext, title: str) -> str:
        """Cancel a scheduled task by its title (or close phrasing).

        Args:
            title: The task to cancel, in the user's words.
        """
        match = await asyncio.to_thread(zoro_tasks.find_by_title, title)
        if not match:
            return f"I couldn't find a task matching {title!r}."
        ok = await asyncio.to_thread(zoro_tasks.cancel, int(match["id"]))
        if not ok:
            return "I found it but couldn't cancel it just now."
        return f"Cancelled — {match['title']}."

    @function_tool()
    @_logged
    async def mark_task_done(self, context: RunContext, title: str) -> str:
        """Mark a task you just carried out as done. Call this after you've
        actually completed a scheduled task that fired — so it doesn't
        keep showing up in the task list.

        Args:
            title: The task you finished, in the user's words.
        """
        match = await asyncio.to_thread(zoro_tasks.find_by_title, title)
        if not match:
            return f"I couldn't find a task matching {title!r}."
        ok = await asyncio.to_thread(zoro_tasks.mark_done, int(match["id"]))
        if not ok:
            return "I couldn't mark that done just now."
        return f"Marked done — {match['title']}."

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def get_weather_report(
        self, context: RunContext, place: str = ""
    ) -> str:
        """Get current weather and today's high/low.

        Args:
            place: A city/place name. Leave empty for the user's current
                location (detected by IP).
        """
        try:
            w = await asyncio.to_thread(
                zoro_tools.get_weather_detailed, place or None
            )
        except Exception as e:  # noqa: BLE001 - report failure aloud
            return f"I couldn't get the weather: {e}"
        await publish_display("weather", w)
        return (
            f"In {w['place']}: currently {w['temp']} degrees, "
            f"{w['condition']}, wind {w['wind']} kilometres per hour. "
            f"Today's high {w['high']} degrees, low {w['low']} degrees."
        )

    @function_tool()
    async def where_am_i(self, context: RunContext) -> str:
        """Tell the user their approximate current location (city, region,
        country, timezone), detected by IP."""
        try:
            return await asyncio.to_thread(zoro_tools.get_location)
        except Exception as e:  # noqa: BLE001
            return f"I couldn't determine your location: {e}"

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def check_calendar(self, context: RunContext, hours: int = 24) -> str:
        """Read the user's upcoming Google Calendar events.

        Use for "what's on my calendar", "am I free this afternoon",
        "what's my day look like". Read-only — just do it.

        Args:
            hours: How far ahead to look (default 24).
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            events = await asyncio.to_thread(zoro_google.agenda, acct, hours)
        except Exception as e:  # noqa: BLE001
            return f"I couldn't reach your calendar: {e}"
        if not events:
            await publish_display(
                "calendar", {"hours": hours, "events": []}
            )
            return f"Nothing on your calendar in the next {hours} hours."
        lines = []
        card_events = []
        for ev in events:
            local = ev["start"].astimezone()
            card_events.append(
                {
                    "summary": ev["summary"],
                    "all_day": ev["all_day"],
                    "start": ev["start"].isoformat(),
                    "when": (
                        local.strftime("%a %b %d")
                        if ev["all_day"]
                        else local.strftime("%a %I:%M %p")
                    ),
                }
            )
        for ev in events[:8]:
            if ev["all_day"]:
                lines.append(
                    f"{ev['summary']} (all day "
                    f"{ev['start'].strftime('%a %b %d')})"
                )
            else:
                lines.append(
                    f"{ev['summary']} at "
                    f"{ev['start'].astimezone().strftime('%a %I:%M %p')}"
                )
        await publish_display(
            "calendar", {"hours": hours, "events": card_events}
        )
        return "Upcoming: " + "; ".join(lines)

    # --- Calendar write ops (Phase 3 new surface) --------------------------

    @function_tool()
    @_logged
    async def create_calendar_event(
        self, context: RunContext, summary: str,
        start_iso: str, end_iso: str, description: str = "",
    ) -> str:
        """Create a calendar event. State-changing — confirm the details
        with the user before calling.

        Convert the user's spoken time into ISO 8601 WITH the local UTC
        offset yourself (e.g. "tomorrow 2pm for an hour" -> start
        2026-05-23T14:00:00+02:00, end 2026-05-23T15:00:00+02:00). Use the
        session's current local time as your anchor for relative phrasing.

        Args:
            summary: Event title.
            start_iso: ISO 8601 start with offset.
            end_iso: ISO 8601 end with offset.
            description: Optional notes/details.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            res = await asyncio.to_thread(
                zoro_google.create_event, acct, summary,
                start_iso, end_iso, description or None,
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't create that event: {e}"
        try:
            when = datetime.fromisoformat(start_iso).astimezone().strftime(
                "%a %b %d at %I:%M %p"
            )
        except ValueError:
            when = start_iso
        return f"Created '{res['summary']}' on {when}."

    @function_tool()
    @_logged
    async def reschedule_calendar_event(
        self, context: RunContext, query: str,
        start_iso: str, end_iso: str,
    ) -> str:
        """Move the upcoming event best matching `query` to a new time.
        State-changing — confirm before calling. Convert the new time to
        ISO 8601 with offset yourself.

        Args:
            query: Which event to move, in the user's words.
            start_iso: New ISO 8601 start with offset.
            end_iso: New ISO 8601 end with offset.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            hits = await asyncio.to_thread(
                zoro_google.find_events, acct, query, 1
            )
            if not hits:
                return f"I couldn't find an upcoming event matching '{query}'."
            await asyncio.to_thread(
                zoro_google.move_event, acct, hits[0]["id"], start_iso, end_iso
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't reschedule that: {e}"
        try:
            when = datetime.fromisoformat(start_iso).astimezone().strftime(
                "%a %b %d at %I:%M %p"
            )
        except ValueError:
            when = start_iso
        return f"Moved '{hits[0]['summary']}' to {when}."

    @function_tool()
    @_logged
    async def cancel_calendar_event(
        self, context: RunContext, query: str
    ) -> str:
        """Delete the upcoming event best matching `query`. State-changing
        and not easily undone — confirm with the user before calling.

        Args:
            query: Which event to cancel, in the user's words.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            hits = await asyncio.to_thread(
                zoro_google.find_events, acct, query, 1
            )
            if not hits:
                return f"I couldn't find an upcoming event matching '{query}'."
            await asyncio.to_thread(
                zoro_google.cancel_event, acct, hits[0]["id"]
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't cancel that: {e}"
        return f"Cancelled '{hits[0]['summary']}'."

    @function_tool()
    @_logged
    async def respond_to_invite(
        self, context: RunContext, query: str, accept: bool
    ) -> str:
        """Accept or decline a calendar invite best matching `query`.
        State-changing (notifies the organiser) — confirm first.

        Args:
            query: Which invite, in the user's words.
            accept: True to accept, False to decline.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            hits = await asyncio.to_thread(
                zoro_google.find_events, acct, query, 1
            )
            if not hits:
                return f"I couldn't find an invite matching '{query}'."
            res = await asyncio.to_thread(
                zoro_google.respond_to_event, acct, hits[0]["id"], accept
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't respond to that invite: {e}"
        return f"{res['status'].capitalize()} '{res['summary']}'."

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def check_email(self, context: RunContext, limit: int = 5) -> str:
        """Read the user's recent unread Gmail (sender + subject).

        Use for "any new email", "check my inbox". Read-only — just do it.

        Args:
            limit: How many unread to summarize (default 5).
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            msgs = await asyncio.to_thread(
                zoro_google.unread_messages, acct, limit
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't reach your inbox: {e}"
        if not msgs:
            await publish_display("inbox", {"messages": []})
            return "Your inbox is all caught up — no unread mail."
        out = []
        card_msgs = []
        for m in msgs:
            frm = m["from"].split("<", 1)[0].strip().strip('"') or m["from"]
            out.append(f"from {frm}: {m['subject']}")
            card_msgs.append(
                {
                    "from": frm,
                    "subject": m["subject"],
                    "snippet": m.get("snippet", ""),
                }
            )
        await publish_display("inbox", {"messages": card_msgs})
        return f"You have {len(msgs)} unread — " + "; ".join(out)

    @function_tool()
    @_logged
    async def send_email(
        self, context: RunContext, to: str, subject: str, body: str
    ) -> str:
        """Send an email from the user's Gmail.

        State-changing: per your safety contract, only call this AFTER you
        have read back the recipient, subject, and gist and the user has
        confirmed.

        Args:
            to: Recipient email address.
            subject: Subject line.
            body: Plain-text body.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            await asyncio.to_thread(
                zoro_google.send_email, acct, to, subject, body
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't send that: {e}"
        return f"Sent to {to}."

    # --- Gmail deep ops (Phase 3 new surface) ------------------------------

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def search_mail(
        self, context: RunContext, query: str, limit: int = 5
    ) -> str:
        """Search Gmail by a natural query across all folders (not just
        unread). Use for "find the email from the bank", "any mail about
        the invoice", "what did Sarah send last week". Read-only.

        Pass a Gmail-style query: plain words work, but you can also use
        operators like from:, subject:, newer_than:7d, has:attachment.

        Args:
            query: What to look for.
            limit: Max messages to summarize (default 5).
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            msgs = await asyncio.to_thread(
                zoro_google.search_messages, acct, query, limit
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't search your mail: {e}"
        if not msgs:
            await publish_display("inbox", {"messages": []})
            return f"No mail matching '{query}'."
        out = []
        card_msgs = []
        for m in msgs:
            frm = m["from"].split("<", 1)[0].strip().strip('"') or m["from"]
            out.append(f"from {frm}: {m['subject']}")
            card_msgs.append({
                "from": frm,
                "subject": m["subject"],
                "snippet": m.get("snippet", ""),
            })
        await publish_display("inbox", {"messages": card_msgs})
        return f"Found {len(msgs)}: " + "; ".join(out)

    @function_tool()
    @_logged
    @zoro_retry.transient()
    async def read_email(self, context: RunContext, query: str) -> str:
        """Read the full body of the email best matching `query` aloud.
        Use for "read me the email from the landlord", "what does the
        invoice email say". Read-only — just do it.

        Args:
            query: Which email to read, in the user's words.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            msg = await asyncio.to_thread(
                zoro_google.find_one_message, acct, query
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't open that email: {e}"
        if not msg:
            return f"I couldn't find an email matching '{query}'."
        frm = msg["from"].split("<", 1)[0].strip().strip('"') or msg["from"]
        body = (msg.get("body") or "").strip()
        await publish_display("inbox", {"messages": [{
            "from": frm, "subject": msg["subject"], "snippet": body[:600],
        }]})
        if not body:
            return f"The email from {frm} about '{msg['subject']}' has no readable text body."
        # Bound the spoken length; the full text is on the card.
        spoken = body if len(body) <= 1200 else body[:1200] + "… that's the gist."
        return f"From {frm}, subject {msg['subject']}: {spoken}"

    @function_tool()
    @_logged
    async def reply_email(
        self, context: RunContext, query: str, body: str
    ) -> str:
        """Reply in-thread to the email best matching `query`.

        State-changing: per your safety contract, only call this AFTER
        you've read back who it's going to and the gist, and the user has
        confirmed.

        Args:
            query: Which email to reply to, in the user's words.
            body: The plain-text reply to send.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            hit = await asyncio.to_thread(
                zoro_google.find_one_message, acct, query
            )
            if not hit:
                return f"I couldn't find an email matching '{query}' to reply to."
            res = await asyncio.to_thread(
                zoro_google.reply_to_message, acct, hit["id"], body
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't send the reply: {e}"
        frm = res["to"].split("<", 1)[0].strip().strip('"') or res["to"]
        return f"Replied to {frm}."

    @function_tool()
    @_logged
    async def archive_email(self, context: RunContext, query: str) -> str:
        """Archive (remove from inbox) the email best matching `query`.

        State-changing — confirm with the user first per your safety
        contract, since it clears the message from the inbox view.

        Args:
            query: Which email to archive, in the user's words.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            hit = await asyncio.to_thread(
                zoro_google.find_one_message, acct, query
            )
            if not hit:
                return f"I couldn't find an email matching '{query}'."
            await asyncio.to_thread(
                zoro_google.archive_message, acct, hit["id"]
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't archive that: {e}"
        return f"Archived the email about '{hit['subject']}'."

    @function_tool()
    @_logged
    async def mark_email_read(
        self, context: RunContext, query: str, read: bool = True
    ) -> str:
        """Mark the email best matching `query` as read (or unread).
        Read-state changes are low-risk — just do it.

        Args:
            query: Which email, in the user's words.
            read: True to mark read, False to mark unread.
        """
        acct = _google_account()
        if not acct:
            return "No Google account is connected."
        try:
            hit = await asyncio.to_thread(
                zoro_google.find_one_message, acct, query
            )
            if not hit:
                return f"I couldn't find an email matching '{query}'."
            await asyncio.to_thread(
                zoro_google.mark_read, acct, hit["id"], read
            )
        except Exception as e:  # noqa: BLE001
            return f"I couldn't update that: {e}"
        return f"Marked '{hit['subject']}' as {'read' if read else 'unread'}."

    @function_tool()
    @_logged
    async def play_music(
        self, context: RunContext, query: str, kind: str = "track"
    ) -> str:
        """Search Spotify and play the top result on the user's active device.

        Use for "play Sicko Mode", "put on some Kendrick", "play the new
        Tyler album". For a known saved playlist use play_my_playlist
        instead. Needs Spotify Premium and an active Spotify client
        (desktop, phone, or web player) running.

        Args:
            query: What to play (track/album/artist/playlist name).
            kind: One of 'track', 'album', 'artist', 'playlist'. Default 'track'.
        """
        try:
            res = await asyncio.to_thread(
                zoro_spotify.search_and_play, query, kind
            )
        except LookupError:
            return f"I couldn't find any {kind} matching {query!r} on Spotify."
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        await publish_now_playing(delay=0.7)
        if res.get("artists"):
            return f"Playing {res['name']} by {res['artists']}."
        return f"Playing {res['name']}."

    @function_tool()
    @_logged
    async def pause_music(self, context: RunContext) -> str:
        """Pause Spotify playback on the active device."""
        try:
            await asyncio.to_thread(zoro_spotify.pause)
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        await publish_now_playing(delay=0.4)
        return "Paused."

    @function_tool()
    @_logged
    async def resume_music(self, context: RunContext) -> str:
        """Resume Spotify playback on the active device."""
        try:
            await asyncio.to_thread(zoro_spotify.play)
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        await publish_now_playing(delay=0.4)
        return "Resumed."

    @function_tool()
    @_logged
    async def skip_track(self, context: RunContext) -> str:
        """Skip to the next track on Spotify."""
        try:
            await asyncio.to_thread(zoro_spotify.next_track)
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        await publish_now_playing(delay=0.7)
        return "Skipped."

    @function_tool()
    @_logged
    async def set_music_volume(
        self, context: RunContext, percent: int
    ) -> str:
        """Set Spotify volume on the active device (0-100).

        Args:
            percent: Target volume 0-100. Values outside clamp.
        """
        try:
            await asyncio.to_thread(zoro_spotify.set_volume, percent)
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        return f"Volume set to {max(0, min(100, int(percent)))}."

    @function_tool()
    @_logged
    async def whats_playing(self, context: RunContext) -> str:
        """Tell the user what's currently playing on Spotify."""
        try:
            cur = await asyncio.to_thread(zoro_spotify.now_playing)
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        await publish_display("nowplaying", cur or {"empty": True})
        if not cur:
            return "Nothing's playing on Spotify right now."
        state = "playing" if cur["is_playing"] else "paused"
        return (
            f"{cur['track']} by {cur['artists']} — {state} on "
            f"{cur['device'] or 'an unknown device'}."
        )

    @function_tool()
    @_logged
    async def like_current_track(self, context: RunContext) -> str:
        """Save the currently-playing Spotify track to the user's Liked Songs."""
        try:
            name = await asyncio.to_thread(zoro_spotify.add_current_to_liked)
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        if not name:
            return "Nothing's playing — I can't save what isn't there."
        await publish_now_playing()
        return f"Saved {name} to your Liked Songs."

    @function_tool()
    @_logged
    async def play_my_playlist(
        self, context: RunContext, name: str
    ) -> str:
        """Find a playlist in the user's library by name and start it.

        Args:
            name: Partial or full playlist name (case-insensitive match).
        """
        try:
            res = await asyncio.to_thread(zoro_spotify.play_playlist, name)
        except LookupError:
            return f"I couldn't find a playlist matching {name!r}."
        except Exception as e:  # noqa: BLE001
            return _spotify_error(e)
        await publish_now_playing(delay=0.9)
        return f"Playing your {res['name']} playlist."

    @function_tool()
    @_logged
    async def morning_briefing(self, context: RunContext) -> str:
        """Gather the morning briefing facts: date & time, location, weather
        (including any rain heads-up), and top news.

        This is the one time the "be brief" rule does NOT apply — the
        morning briefing is meant to be a full, unhurried briefing. Deliver
        it as a warm, beautiful good-morning in your own voice: natural,
        flowing, conversational prose — never a read-out list.

        Take your time and let each segment breathe. Do NOT rush from one
        part to the next or compress everything into a sentence or two.
        Give each segment its own moment with a real, natural transition
        between them (a short connective remark, not "next,"/"moving on"):

        - Open with a genuine, warm greeting and how you hope they slept.
        - The date and time, and a word about the day itself.
        - Where they are, then the weather with actual substance — current
          conditions, the high and low and what that will feel like through
          the day, and weave the rain heads-up in naturally (e.g. "you may
          want an umbrella around two").
        - What their day looks like: if Google Calendar tools are
          available, fetch today's events and walk through them
          unhurriedly; otherwise use what you know from memory/profile.
        - The news: don't just list headlines — give each a sentence of
          context so it actually informs, then round off.
        - If SELF_IMPROVEMENT is not "(none today)": weave that paragraph
          in naturally before closing — speak it as your own reflection
          in your own voice (you wrote it last night), not as a
          read-out. Tell Skhathi what you learned about yourself and
          what you'd like to improve. If the report mentions resources
          you need, ask for them plainly.
        - Close warmly and hand the day back to them.

        Address the user as "Sir". Warmth and completeness over brevity
        here; this should feel like a full briefing, not a summary.
        """
        now = datetime.now()
        when = now.strftime("%A, %B %d, %Y — the time is %I:%M %p")
        # Each part is best-effort: a flaky network or a rate-limited IP
        # lookup must never sink the whole morning greeting. Date/time
        # alone is still a usable briefing.
        try:
            locd = await asyncio.to_thread(zoro_tools.get_location_detailed)
            loc = (
                f"You appear to be in {locd['city']}, {locd['region']}, "
                f"{locd['country']}. Timezone {locd['timezone']}."
            )
        except Exception:  # noqa: BLE001
            locd = None
            loc = "Location unavailable right now."
        try:
            wxd = await asyncio.to_thread(
                zoro_tools.get_morning_weather_detailed
            )
            wx = (
                f"In {wxd['place']}, it's currently {wxd['temp']} degrees "
                f"and {wxd['condition']}. Today's high is {wxd['high']}, "
                f"low {wxd['low']} degrees. {wxd['rain']}"
            )
        except Exception:  # noqa: BLE001
            wxd = None
            wx = "Weather unavailable right now."
        try:
            news = await asyncio.to_thread(zoro_tools.get_news_detailed, 4)
        except Exception:  # noqa: BLE001
            news = []
        news_txt = (
            " || ".join(n["title"] for n in news)
            if news
            else "News unavailable right now."
        )
        await publish_display(
            "briefing",
            {
                "time": when,
                "location": locd,
                "weather": wxd,
                "news": news,
            },
        )
        # Self-improvement report: a short paragraph the nightly
        # reflector wrote last night about what Zoro learned and what
        # she'd like to do better. Only includes the most recent
        # unspoken one; marked spoken here so it isn't repeated.
        self_report_txt = ""
        try:
            rpt = await asyncio.to_thread(zoro_reflector.unspoken_report)
        except Exception:  # noqa: BLE001
            rpt = None
        if rpt and rpt.get("summary"):
            self_report_txt = rpt["summary"].strip()
            rid = rpt.get("id")
            if rid:
                asyncio.create_task(asyncio.to_thread(
                    zoro_reflector.mark_spoken, int(rid),
                ))
        return (
            f"DATE_TIME: {when}\n"
            f"LOCATION: {loc}\n"
            f"WEATHER: {wx}\n"
            f"TOP_NEWS: {news_txt}\n"
            f"SELF_IMPROVEMENT: {self_report_txt or '(none today)'}"
        )


_ROOT = Path(__file__).resolve().parent.parent


def _google_account() -> str:
    """The Gmail/Calendar account Zoro acts on, from zoro_config.json."""
    try:
        return zoro_safety.load_config().get("gmail_account", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _youtube_error(exc: Exception) -> str:
    """Translate YouTube/Data-API failures into a spoken sentence."""
    import httpx as _httpx

    msg = str(exc).strip()
    if isinstance(exc, _httpx.HTTPStatusError):
        body = ""
        try:
            body = exc.response.text[:300]
        except Exception:  # noqa: BLE001
            pass
        if exc.response.status_code == 403:
            if "quota" in body.lower():
                return (
                    "YouTube says I'm out of daily quota — try again "
                    "tomorrow or raise the limit in Google Cloud."
                )
            if "api key not valid" in body.lower():
                return (
                    "YouTube rejected the API key. Check "
                    "YOUTUBE_API_KEY in .env."
                )
        return f"YouTube API error {exc.response.status_code}."
    if "YOUTUBE_API_KEY" in msg:
        return msg
    snippet = msg[:160] + ("…" if len(msg) > 160 else "")
    return f"YouTube error: {snippet}"


def _whatsapp_error(exc: Exception) -> str:
    """Translate WhatsApp bridge failures into a spoken sentence."""
    import httpx as _httpx

    if isinstance(exc, _httpx.ConnectError):
        return (
            "The WhatsApp bridge isn't running. Start it under the "
            "supervisor, or run services/whatsapp/bridge.js once to "
            "scan the QR."
        )
    if isinstance(exc, _httpx.TimeoutException):
        return "The WhatsApp bridge timed out."
    msg = str(exc).strip()
    snippet = msg[:160] + ("…" if len(msg) > 160 else "")
    return f"WhatsApp error: {snippet}"


def _gh_error(exc: Exception) -> str:
    """Translate gh CLI failures into something Zoro can say aloud."""
    msg = str(exc).strip()
    low = msg.lower()
    if "no such file" in low or "not recognized" in low or (
        "no such" in low and "gh" in low
    ):
        return (
            "The GitHub CLI isn't installed. Run `winget install "
            "GitHub.cli` then `gh auth login`."
        )
    if "not logged" in low or "authentication" in low or "401" in low:
        return "GitHub says I'm not authenticated. Run `gh auth login`."
    if "not found" in low and "repository" in low:
        return "GitHub couldn't find that repository."
    # Truncate so a 5kb gh error doesn't fill the spoken reply.
    snippet = msg[:160] + ("…" if len(msg) > 160 else "")
    return f"GitHub error: {snippet}"


def _spotify_error(exc: Exception) -> str:
    """Translate spotipy failures into a sentence Zoro can speak aloud."""
    # Keep spotipy optional: importing this module should still work when the
    # Spotify integration's dependency is not installed.
    try:
        spotify_exceptions = importlib.import_module("spotipy.exceptions")
        spotify_exception = spotify_exceptions.SpotifyException
    except (ImportError, AttributeError):
        spotify_exception = ()

    if isinstance(exc, spotify_exception):
        status = getattr(exc, "http_status", None)
        reason = (getattr(exc, "reason", "") or "").upper()
        msg = str(exc)
        if status == 403:
            if "PREMIUM_REQUIRED" in reason or "PREMIUM_REQUIRED" in msg:
                return (
                    "Spotify rejected that — playback control needs a "
                    "Premium account."
                )
            if "VOLUME_CONTROL_DISALLOW" in reason or "VOLUME_CONTROL_DISALLOW" in msg:
                return (
                    "Spotify won't let me set the volume on that device — "
                    "switch to the desktop app or a speaker, then try again."
                )
            if "DEVICE_NOT_CONTROLLABLE" in reason or "DEVICE_NOT_CONTROLLABLE" in msg:
                return (
                    "That Spotify device doesn't accept remote control. "
                    "Open Spotify on your desktop or pick a different device."
                )
            return "Spotify refused that command for this device."
        if status == 404:
            return (
                "Spotify has no active device. Open Spotify on your phone "
                "or desktop and try again."
            )
        if status == 401:
            return (
                "Spotify says my authorisation has expired. Re-run the "
                "Spotify auth script."
            )
    msg = str(exc).strip() or exc.__class__.__name__
    return f"Spotify error: {msg}"


def _consume_pending_directive() -> tuple[str | None, str | None]:
    """If the proactive service queued something for Zoro to say, return
    (instruction, raw_text) and clear it from Azure Redis (and local mirror).
    Stale directives (>10 min) are dropped so she never opens with something that's
    no longer relevant. raw_text is the human-facing line, used for the proactive toast card.
    """
    return zoro_safety.consume_pending_directive()


def morning_directive() -> tuple[str, str | None]:
    """Greeting instruction for session start, plus the proactive toast
    text (or None). A queued proactive directive wins; otherwise the first
    morning wake each day gives the full briefing, and anything else gets a
    normal short greeting.
    """
    proactive, proactive_text = _consume_pending_directive()
    if proactive:
        return proactive, proactive_text
    state = _ROOT / "data" / "last_briefing.txt"
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    is_morning = 4 <= now.hour < 12
    last = ""
    try:
        last = state.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if is_morning and last != today:
        try:
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(today, encoding="utf-8")
        except OSError:
            pass
        return (
            "It is morning and the user has just woken you for the first "
            "time today. Immediately use the morning_briefing tool and "
            "deliver its contents as a warm, beautiful good-morning greeting "
            "in your own voice. Give the FULL, unhurried briefing — take "
            "your time, let each segment breathe with natural transitions, "
            "and do not rush from one part to the next."
        ), None
    return (
        "Greet the user briefly as Zoro, addressing them appropriately "
        "(Sir, or by name if natural), and ask how you can help."
    ), None


async def entrypoint(ctx: JobContext) -> None:
    # Pass keys explicitly — plugin env-var names differ from our .env
    # names, and STT and TTS ride on Azure AI Services by default.
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-6-sol")
    azure_speech_region = os.environ.get("AZURE_SPEECH_REGION", "eastus2")
    azure_speech_voice = os.environ.get("AZURE_SPEECH_VOICE", "en-GB-AdaMultilingualNeural")

    if azure_endpoint and azure_key:
        active_llm = openai.LLM.with_azure(
            model=azure_deployment,
            azure_endpoint=azure_endpoint,
            azure_deployment=azure_deployment,
            api_key=azure_key,
            api_version=os.environ.get("OPENAI_API_VERSION", "2024-10-21"),
            max_completion_tokens=2048,
            reasoning_effort="none",
        )
        llm_label = f"azure-openai({azure_deployment})"
    else:
        active_llm = anthropic.LLM(
            model="claude-sonnet-4-5",
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            caching="ephemeral",
            _strict_tool_schema=False,
            max_tokens=2048,
            timeout=httpx.Timeout(15.0, read=60.0, write=15.0, pool=15.0),
        )
        llm_label = "anthropic(sonnet-4-5)"

    # Audio Pipeline: 100% Microsoft Azure AI Speech (Deepgram & Cartesia eradicated)
    active_stt = azure.STT(
        language="en-US",
        speech_key=azure_key or "",
        speech_region=azure_speech_region,
    )
    active_tts = azure.TTS(
        voice=azure_speech_voice,
        speech_key=azure_key,
        speech_region=azure_speech_region,
    )
    stt_label = f"azure-speech-stt({azure_speech_region})"
    tts_label = f"azure-neural-tts({azure_speech_voice})"

    session = AgentSession(
        stt=active_stt,
        llm=active_llm,
        tts=active_tts,
        vad=_VAD,
        turn_handling={
            "endpointing": {"min_delay": 0.25, "max_delay": 3.0},
            "interruption": {
                "enabled": True,
                "mode": "adaptive",
                "min_duration": 0.5,
                "resume_false_interruption": True,
                "false_interruption_timeout": 3.0,
                "discard_audio_if_uninterruptible": True,
            },
            "preemptive_generation": {"enabled": True, "preemptive_tts": True},
        },
    )

    print(f"[zoro] session config: TTS={tts_label} "
          f"STT={stt_label} LLM={llm_label} MCP=off",
          flush=True)
    instructions = await asyncio.to_thread(build_instructions)

    # Correction detector: when the user's next final utterance looks like
    # "no / wrong / try again / I meant…", flip the most recent successful
    # tool episode from outcome='success' to 'corrected'. Closes the
    # telemetry loop — silent wrong-answers stop reading as wins.
    @session.on("user_input_transcribed")
    def _on_user_input(ev) -> None:
        if not getattr(ev, "is_final", False):
            return
        text = (getattr(ev, "transcript", "") or "").strip()
        if not text or not zoro_corrections.is_correction(text):
            return
        ep = _consume_correctable_episode()
        if not ep:
            return
        # Remove so we don't re-flag the same row on a follow-up "no no no".
        try:
            _RECENT_EPISODES.remove(ep)
        except ValueError:
            pass
        print(f"[zoro] correction detected on episode {ep['id']} "
              f"({ep['tool']}): {text!r}", flush=True)
        asyncio.create_task(asyncio.to_thread(
            zoro_episodes.mark_corrected, ep["id"], text
        ))

    # Interrupt-quality telemetry: every false-interruption is a friction
    # point (Zoro stopped mid-sentence for nothing). Logged as a synthetic
    # episode so it shows up in the flakiness report.
    @session.on("agent_false_interruption")
    def _on_false_interrupt(ev) -> None:
        resumed = bool(getattr(ev, "resumed", False))
        # A resumed false-interrupt is a non-event: Zoro twitched and
        # auto-recovered the same sentence, so it's a success, not a
        # failure. Only a false-interrupt she did NOT recover from
        # (resumed=False) is real friction worth surfacing. Logging the
        # resumed ones as success keeps the *rate* visible for tuning
        # without flooding the failure report or skewing reflection.
        asyncio.create_task(asyncio.to_thread(
            zoro_episodes.log_episode,
            kind="voice",
            tool="false_interruption",
            task="agent stopped speaking but the user wasn't really talking",
            outcome="success" if resumed else "aborted",
            result_summary=f"resumed={resumed}",
            session_id=_SESSION_ID,
        ))

    zoro_telemetry.track_event('zoro_session_start', {'room': ctx.room.name, 'llm': llm_label, 'session_id': _SESSION_ID})
    agent = Zoro(instructions)
    zoro_schema_cache.warm_cache_for_agent(agent)
    await session.start(agent=agent, room=ctx.room)

    @ctx.room.on("data_received")
    def _on_room_data(dp) -> None:
        try:
            raw = dp.data.decode("utf-8") if isinstance(dp.data, (bytes, bytearray)) else str(dp.data)
            payload = json.loads(raw)
            action = payload.get("action")
            if action == "browser_resume":
                print("[zoro] client triggered browser_resume via data channel", flush=True)

                async def _resume_job():
                    b = zoro_browser.get_browser()
                    r = await b.resume_from_user()
                    await publish_display("browser", r)
                    await session.generate_reply(
                        instructions=(
                            "The user clicked 'Hand Back to Zoro' after finishing login on the desktop browser. "
                            f"Briefly confirm in one sentence that you have resumed autonomous control of {r.get('title', 'the browser')}, "
                            "and ask what they'd like you to do next."
                        )
                    )

                asyncio.create_task(_resume_job())
            elif action == "browser_handoff":
                print("[zoro] client triggered browser_handoff via data channel", flush=True)

                async def _handoff_job():
                    b = zoro_browser.get_browser()
                    r = await b.handoff_to_user(payload.get("reason", "User requested takeover"))
                    await publish_display("browser", r)

                asyncio.create_task(_handoff_job())
        except Exception as e:
            print(f"[zoro] room data handling skipped: {e}", flush=True)

    # Follow Spotify in the background so the now-playing card stays live
    # when playback moves on its own (a track finishing, or the user
    # pausing/skipping from the Spotify app). Cancelled on job shutdown so
    # it never outlives the session.
    spotify_task = asyncio.create_task(_watch_spotify())

    async def _stop_spotify_watch() -> None:
        spotify_task.cancel()
        try:
            await spotify_task
        except asyncio.CancelledError:
            pass

    ctx.add_shutdown_callback(_stop_spotify_watch)

    directive, proactive_text = morning_directive()
    if proactive_text:
        await publish_display("proactive", {"text": proactive_text})
    await session.generate_reply(instructions=directive)


if __name__ == "__main__":
    # Single-user setup: livekit's prod defaults (4 idle worker processes,
    # load_threshold=0.7) saturate this CPU and starve Kokoro mid-utterance,
    # causing audible breaks. Keep one idle process for instant pickup and
    # override load_fnc so high Windows background CPU (95%+) never marks
    # Zoro's worker as "unavailable" when the user clicks the orb or wakes her.
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        num_idle_processes=1,
        load_fnc=lambda: 0.1,
        load_threshold=float("inf"),
    ))
