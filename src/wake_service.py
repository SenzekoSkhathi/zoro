"""Zoro on-device wake-word service.

Listens to the local microphone with openWakeWord (runs entirely on-device,
no account, near-instant) and notifies the browser frontend over a local
WebSocket the moment the wake word is heard.

  mic ─► openWakeWord ─► ws://127.0.0.1:8765 ─► browser connects to Zoro

The browser can send {"cmd": "pause"} while a call is active so Zoro's own
voice doesn't re-trigger the wake word, then {"cmd": "resume"} when it ends.

Run:
    .venv/Scripts/python.exe src/wake_service.py
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed
from openwakeword.model import Model

_ROOT = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _app_exe() -> Path | None:
    """Locate the Zoro desktop shell exe (installed or dev build), if any."""
    override = os.environ.get("ZORO_APP_EXE")
    if override and Path(override).is_file():
        return Path(override)
    for c in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Zoro" / "Zoro.exe",
        Path("C:/Program Files/Zoro/Zoro.exe"),
        _ROOT / "desktop" / "src-tauri" / "target" / "release" / "zoro-desktop.exe",
    ):
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _summon_app(flag: str = "--wake") -> None:
    """Surface the Zoro desktop app on wake and trigger a session. Launching
    the exe routes through the app's single-instance handler, which shows the
    window and (because of `flag`) calls the page's wake bridge — the native
    path that replaces the ws:// wake the hosted HTTPS UI can't use. Best-
    effort and a no-op until the app is installed; browser users are
    unaffected since the mic wake is laptop-local.
    """
    exe = _app_exe()
    if exe is None:
        print("[wake] summon skipped — desktop app not found", flush=True)
        return
    try:
        subprocess.Popen(
            [str(exe), flag],
            creationflags=_NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[wake] summoned desktop app ({flag})", flush=True)
    except OSError as e:
        print(f"[wake] summon failed: {e}", flush=True)

# Default to the trained custom "hey_zoro" model if present, else fall back to
# openWakeWord's built-in "hey_jarvis". Override via the env var if needed.
_CUSTOM = Path(__file__).resolve().parent.parent / "models" / "hey_zoro.onnx"
WAKE_MODEL = os.environ.get("ZORO_WAKE_MODEL") or (
    str(_CUSTOM) if _CUSTOM.exists() else "hey_jarvis"
)
THRESHOLD = float(os.environ.get("ZORO_WAKE_THRESHOLD", "0.5"))
COOLDOWN_S = 2.0            # ignore repeat detections within this window
HOST = "127.0.0.1"
PORT = 8765

SAMPLE_RATE = 16000
FRAME = 1280                # 80 ms @ 16 kHz — openWakeWord's expected chunk

# A call-pause is a *lease*, not a latch: the browser refreshes it every few
# seconds while a call is live, and it auto-expires PAUSE_TTL_S after the last
# refresh. This is the whole robustness story — a client that crashes, has its
# ws connection churn (the hosted HTTPS UI's ws:// to loopback drops without a
# close frame), or closes its tab mid-call can no longer leave the mic
# permanently deaf, because the lease simply lapses and listening resumes.
# Must comfortably exceed the browser's refresh interval (~4 s).
PAUSE_TTL_S = 8.0

clients: set = set()
# Deadline (time.monotonic) until which the call-pause lease mutes the mic; 0
# means no active lease. The tray's "Pause listening" is a *separate*, sticky
# mute (no TTL) so the user can intentionally silence Zoro indefinitely.
_pause_until = 0.0
_muted_sticky = False
_pause_logged = False      # so per-refresh leases don't spam the log


def _set_call_pause(active: bool) -> None:
    """Refresh (active) or clear (active=False) the auto-expiring call-pause
    lease. Logs only on the leading edge so keepalive refreshes stay quiet."""
    global _pause_until, _pause_logged
    if active:
        _pause_until = time.monotonic() + PAUSE_TTL_S
        if not _pause_logged:
            print("[wake] paused (call active)", flush=True)
            _pause_logged = True
    else:
        _pause_until = 0.0
        if _pause_logged:
            print("[wake] resumed (call ended)", flush=True)
            _pause_logged = False


def _is_muted() -> bool:
    """True while the mic should ignore audio: either the tray's sticky mute
    or a live call-pause lease. A lease that lapses without an explicit resume
    (client vanished) resumes listening on its own."""
    global _pause_logged
    if _muted_sticky:
        return True
    if time.monotonic() < _pause_until:
        return True
    if _pause_logged:
        print("[wake] resumed (pause lease expired)", flush=True)
        _pause_logged = False
    return False


async def _handler(ws):
    clients.add(ws)
    print(f"[wake] client connected ({len(clients)} total)")
    try:
        async for raw in ws:
            try:
                cmd = json.loads(raw).get("cmd")
            except (ValueError, AttributeError):
                continue
            if cmd == "pause":
                # Lease refresh: the browser sends this on call start and then
                # repeatedly while the call is live (see useWakeService).
                _set_call_pause(True)
            elif cmd == "resume":
                _set_call_pause(False)
            elif cmd == "mute":
                # Tray "Pause listening": sticky, indefinite, user-intentional.
                global _muted_sticky
                _muted_sticky = True
                print("[wake] muted (tray)")
            elif cmd == "unmute":
                _muted_sticky = False
                print("[wake] unmuted (tray)")
            elif cmd == "proactive":
                # The proactive service asks us to wake the browser exactly
                # as a spoken wake word would, so it connects and the agent
                # delivers the queued directive.
                print("[wake] proactive trigger -> notifying browser")
                _broadcast_proactive(asyncio.get_running_loop(), source_ws=ws)
    except ConnectionClosed:
        # The hosted UI's ws:// to loopback frequently drops without a close
        # handshake; that's expected, not an error. Swallow it so we don't log
        # a "connection handler failed" traceback on every reconnect.
        pass
    finally:
        clients.discard(ws)
        # No pause cleanup needed here anymore: the call-pause is a lease that
        # lapses on its own (PAUSE_TTL_S), so a vanished client can't leave the
        # mic deaf even if it never sent a resume.
        print(f"[wake] client disconnected ({len(clients)} total)")


def _broadcast_proactive(loop, source_ws=None):
    """Notify all connected web clients to connect to Zoro for a proactive directive."""
    _summon_app("--proactive")
    msg = json.dumps({"event": "proactive"})
    for ws in list(clients):
        if ws is not source_ws:
            loop.create_task(ws.send(msg))


def _broadcast_wake(loop, name):
    # Scheduled onto the loop via call_soon_threadsafe, so this already
    # runs IN the loop thread. run_coroutine_threadsafe is the cross-thread
    # primitive and is the wrong tool here (it only worked by accident
    # because the future was never awaited); create_task is correct.
    # Surface the desktop app (if installed) so its window appears even when
    # it was sitting hidden in the tray; the connected webview handles the
    # session as before.
    _summon_app("--wake")
    msg = json.dumps({"event": "wake", "model": name})
    for ws in list(clients):
        loop.create_task(ws.send(msg))


def _redis_proactive_loop(loop):
    """Listen for proactive events emitted via Azure Redis across distributed workers."""
    try:
        import zoro_safety
        r = zoro_safety._get_redis()
        if not r:
            return
        pubsub = r.pubsub()
        pubsub.subscribe("zoro:proactive:events")
        print("[wake] subscribed to Azure Redis zoro:proactive:events", flush=True)
        for message in pubsub.listen():
            if message and message.get("type") == "message":
                print("[wake] received proactive event from Azure Redis -> notifying browser", flush=True)
                loop.call_soon_threadsafe(_broadcast_proactive, loop)
    except Exception as e:
        print(f"[wake] redis proactive subscriber ended: {e}", flush=True)


def _audio_loop(loop):
    model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    last_fire = 0.0
    print(f"[wake] listening for '{WAKE_MODEL}' (threshold {THRESHOLD})")
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME
    ) as stream:
        while True:
            data, _ = stream.read(FRAME)
            if _is_muted():
                continue
            scores = model.predict(np.squeeze(data))
            # Use whatever key the model exposes (a custom .onnx is keyed by
            # its filename), so swapping models needs no code change.
            name, score = max(scores.items(), key=lambda kv: kv[1])
            now = time.time()
            if score >= THRESHOLD and (now - last_fire) > COOLDOWN_S:
                last_fire = now
                print(f"[wake] DETECTED {name} ({score:.2f}) -> notifying browser")
                loop.call_soon_threadsafe(_broadcast_wake, loop, name)


async def main():
    loop = asyncio.get_running_loop()
    threading.Thread(target=_audio_loop, args=(loop,), daemon=True).start()
    threading.Thread(target=_redis_proactive_loop, args=(loop,), daemon=True).start()
    async with websockets.serve(_handler, HOST, PORT):
        print(f"[wake] WebSocket up on ws://{HOST}:{PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[wake] stopped")
