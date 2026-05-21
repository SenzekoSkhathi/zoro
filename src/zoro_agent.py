"""Zoro — voice-first assistant agent.

Phase 1: desktop, push-to-talk. Pipeline:
  mic -> Deepgram (STT) -> Claude (brain) -> Deepgram Aura (TTS) -> speakers

Run locally in console mode (no phone / web UI needed yet):
    .venv/Scripts/python.exe src/zoro_agent.py console
"""

import asyncio
import functools
import inspect
import json
import os
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
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import httpx
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)
from livekit.plugins import anthropic, cartesia, deepgram, silero

import zoro_claude_code
import zoro_computer
import zoro_corrections
import zoro_episodes
import zoro_lessons
import zoro_github
import zoro_google
import zoro_memory
import zoro_safety
import zoro_shell
import zoro_retry
import zoro_spotify
import zoro_tasks
import zoro_tools
import zoro_whatsapp
import zoro_youtube
from zoro_prompt import ZORO_SYSTEM_PROMPT

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
_VAD = silero.VAD.load()


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


async def publish_display(kind: str, data: dict) -> None:
    """Push a content card to the web view over the LiveKit data channel.

    Best-effort: a missing room (console mode) or any transport hiccup
    must never break the spoken reply, so all failures are swallowed.
    """
    try:
        room = get_job_context().room
        payload = json.dumps({"kind": kind, "data": data})
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
    """One spoken sentence summarising how a Claude session ended.

    Trims long results so Zoro doesn't read paragraphs aloud — the full
    text stays on the card for the user to read.
    """
    raw = (state.get("result") or "").strip().replace("\n", " ")
    merge = state.get("merge") or ""
    if state.get("status") == "failed":
        snippet = raw[:140] + ("…" if len(raw) > 140 else "")
        return f"Claude couldn't finish: {snippet}" if snippet else (
            "Claude hit an error."
        )
    if merge == "rolled-back":
        return (
            "Claude's change broke my boot check, so I rolled it back. "
            "Nothing was merged, Sir."
        )
    if merge == "no-changes":
        return "Claude ran but didn't change anything."
    snippet = raw[:200] + ("…" if len(raw) > 200 else "")
    suffix = " Merged into main." if merge == "merged" else ""
    if snippet:
        return f"Claude finished. {snippet}{suffix}"
    return "Claude is done." + suffix


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
        import zoro_proactive

        zoro_proactive._fire(text, f"claude_done_{int(time.time())}")
    except Exception as e:  # noqa: BLE001
        print(f"[zoro] claude announce skipped: {e}", flush=True)


async def _run_claude_task(task: str, session=None) -> None:
    """Stream a Claude Code session into the web view as it runs, then
    speak a one-line summary when it finishes.

    The card replaces in place on each event; a single "claude" widget
    holds the latest task and its progress log.
    """
    state: dict = {
        "task": task,
        "status": "running",
        "lines": [],
        "result": "",
    }
    await publish_display("claude", state)
    try:
        async for ev in zoro_claude_code.run_streaming(task):
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
        await publish_display("claude", state)
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


_NO_REFLECT_TOOLS = frozenset({
    "read_my_source",
    "read_my_notes",
    "what_time_is_it",
})


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
    """Render a tiny 'recent attempts' summary for the system prompt. Best-
    effort: empty string if Supabase is unreachable or there's no history."""
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
        """Open an application on the user's Windows computer.

        Args:
            app_name: The app the user wants opened, e.g. "notepad",
                "calculator", "chrome", "settings". Spoken names are fine.
        """
        key = app_name.strip().lower()
        target = APP_ALIASES.get(key, key)
        try:
            # `start` resolves apps via Windows App Paths / PATH. Args are
            # passed as a list (no shell=True) so the app name isn't a shell
            # injection vector; the empty "" is start's window-title arg.
            subprocess.Popen(["cmd", "/c", "start", "", target])
        except OSError as e:
            return f"I couldn't open {app_name}: {e}"
        return f"Opening {app_name}."

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
                os.environ["ANTHROPIC_API_KEY"],
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
            "everything": "all", "clear": "all", "all widgets": "all",
        }
        t = aliases.get(t, t)
        valid = {
            "search", "calendar", "inbox", "weather", "briefing",
            "notes", "proactive", "vision", "chat", "all",
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
    @_logged
    async def control_computer(
        self, context: RunContext, task: str
    ) -> str:
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
                zoro_computer.run_task, task, os.environ["ANTHROPIC_API_KEY"]
            )
        except Exception as e:  # noqa: BLE001 - report aloud
            return f"My computer control failed: {e}"

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
        """Open an installed Windows app by name.

        Fuzzy-matches against the Start Menu, so casual names like
        "spotify", "discord", "chrome", "vscode" all work. Use for
        any "open X" / "launch X" voice command.

        Args:
            name: App name. Partial / fuzzy match is fine.
        """
        try:
            match = await asyncio.to_thread(zoro_shell.open_app, name)
        except LookupError:
            return f"I couldn't find an installed app matching {name!r}."
        except Exception as e:  # noqa: BLE001
            return f"I couldn't open it: {e}"
        return f"Opening {match['name']}."

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
        self, context: RunContext, task: str
    ) -> str:
        """Hand a coding/editing task off to Claude Code in the Zoro repo.

        Fire-and-forget: the spawned `claude` CLI session runs with
        permissions bypassed in `c:/Users/senze/Downloads/zoro`, streams
        its progress to a web card, and the user reviews the diff
        afterwards.

        Use for things Zoro herself can't do: writing/refactoring code,
        adding features, fixing bugs in the repo. NOT for trivial file
        edits she could do via her own file tools — this spins up a
        whole Claude session.

        Args:
            task: Plain-language task, e.g. "add a dark-mode toggle to
                the web app" or "fix the import error in zoro_proactive".
        """
        asyncio.create_task(_run_claude_task(task, context.session))
        return (
            "I'll have Claude work on that — watch the screen for "
            "progress. I'll let you know when it's done."
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
        return (
            f"DATE_TIME: {when}\n"
            f"LOCATION: {loc}\n"
            f"WEATHER: {wx}\n"
            f"TOP_NEWS: {news_txt}"
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
    from spotipy.exceptions import SpotifyException

    if isinstance(exc, SpotifyException):
        status = exc.http_status
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
    (instruction, raw_text) and clear it. Stale directives (>10 min) are
    dropped so she never opens with something that's no longer relevant.
    raw_text is the human-facing line, used for the proactive toast card.
    """
    import json

    pending = _ROOT / "data" / "pending_directive.json"
    try:
        d = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    try:
        pending.unlink()
    except OSError:
        pass
    if (datetime.now().timestamp() - float(d.get("ts", 0))) > 600:
        return None, None
    text = (d.get("text") or "").strip()
    if not text:
        return None, None
    return (
        "You are reaching out to the user proactively (not answering a "
        "question). Deliver this naturally in your own voice as Zoro, in "
        "one or two short spoken sentences, then stop:\n" + text
    ), text


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
    # names, and STT and TTS both ride on the single DEEPGRAM_API_KEY.
    session = AgentSession(
        stt=deepgram.STT(
            model="nova-3", language="en", api_key=os.environ["DEEPGRAM_API_KEY"]
        ),
        llm=anthropic.LLM(
            model="claude-sonnet-4-5",
            api_key=os.environ["ANTHROPIC_API_KEY"],
            # Cache the (large) system prompt + tool schemas + history so
            # every turn after the first is small and fast — fixes the
            # first-token timeouts caused by the heavy MCP tool surface.
            caching="ephemeral",
            # Zoro has >20 tools; Anthropic caps *strict* tools at 20, so
            # disable strict tool schemas (tool calling still works fine —
            # this is the pre-strict default behaviour). Without this every
            # LLM call 400s with "Too many strict tools" and she goes mute.
            _strict_tool_schema=False,
            # Voice replies are short, but max_tokens is a ceiling, not a
            # target — normal turns still stop early, so a higher cap costs
            # nothing for them while letting the full morning briefing run
            # to completion instead of being truncated mid-sentence.
            max_tokens=2048,
            # Generous HTTP timeout for slower networks/large first request.
            timeout=httpx.Timeout(15.0, read=60.0, write=15.0, pool=15.0),
        ),
        # Local Kokoro-FastAPI (OpenAI-compatible) — runs at localhost:8880,
        # zero API cost. Voice bf_alice = British female. If the local server
        # is down, falls back to Deepgram Aura.
        # Cartesia Sonic — cloud TTS with native streaming. Replaced local
        # Kokoro because CPU inference couldn't sustain realtime on this box
        # and caused mid-sentence underruns. Voice: Victoria (British female,
        # crisp/professional). Sonic-2 is Cartesia's lowest-latency model.
        tts=cartesia.TTS(
            model="sonic-2",
            voice="dc30854e-e398-4579-9dc8-16f6cb2c19b9",
            api_key=os.environ["CARTESIA_API_KEY"],
        ),
        vad=_VAD,
        # Phase 2 barge-in polish: migrated off the deprecated flat kwargs
        # onto the TurnHandlingOptions TypedDict (single source of truth for
        # turn behaviour going forward). Snappier interrupt min_duration
        # than the default 0.5s so the user doesn't have to talk over Zoro
        # for half a second to cut her off; adaptive mode lets the SDK
        # distinguish real interrupts from short backchannels ("mm-hm").
        turn_handling={
            "endpointing": {"min_delay": 0.3, "max_delay": 3.0},
            "interruption": {
                "enabled": True,
                "mode": "adaptive",
                "min_duration": 0.35,
                # Resume mid-sentence if the apparent interrupt turned out
                # to be a cough / backchannel within the timeout window.
                "resume_false_interruption": True,
                "false_interruption_timeout": 2.0,
                "discard_audio_if_uninterruptible": True,
            },
            "preemptive_generation": {"enabled": True, "preemptive_tts": False},
        },
    )

    print("[zoro] session config: TTS=cartesia(sonic-2/Gemma) "
          "STT=deepgram(nova-3) LLM=anthropic(sonnet-4-5) MCP=off",
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
        asyncio.create_task(asyncio.to_thread(
            zoro_episodes.log_episode,
            kind="voice",
            tool="false_interruption",
            task="agent stopped speaking but the user wasn't really talking",
            outcome="error" if not resumed else "aborted",
            result_summary=f"resumed={resumed}",
            session_id=_SESSION_ID,
        ))

    await session.start(agent=Zoro(instructions), room=ctx.room)
    directive, proactive_text = morning_directive()
    if proactive_text:
        await publish_display("proactive", {"text": proactive_text})
    await session.generate_reply(instructions=directive)


if __name__ == "__main__":
    # Single-user setup: livekit's prod defaults (4 idle worker processes,
    # load_threshold=0.7) saturate this CPU and starve Kokoro mid-utterance,
    # causing audible breaks. Keep one idle process for instant pickup and
    # raise the load gate so the worker doesn't churn "unavailable/available"
    # while Kokoro is synthesizing.
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        num_idle_processes=1,
        load_threshold=0.95,
    ))
