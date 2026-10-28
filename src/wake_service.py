"""Zoro on-device wake-word service.

Listens to the local microphone with openWakeWord (runs entirely on-device,
no account, near-instant) and notifies the browser frontend over a local
WebSocket the moment the wake word is heard.

  mic ─► openWakeWord ─► ws://127.0.0.1:8765 ─► browser connects to Zoro

The browser can send {"cmd": "pause"} while a call is active so Zoro's own
voice doesn't re-trigger the wake word, then {"cmd": "resume"} when it ends.

Includes Windows CoreAudio output metering so active media playback (movies,
music, YouTube, TTS output) suppresses false-positive wake triggers from
speaker bleed into the microphone.

Run:
    .venv/Scripts/python.exe src/wake_service.py
"""

from collections import deque
import asyncio
import ctypes
from ctypes import POINTER, Structure, byref, c_float, c_uint, c_ulong, c_void_p
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent
_ROOT = _SRC.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

try:
    zoro_safety = importlib.import_module("zoro_safety")
except ImportError:
    # Redis-backed proactive wake is optional; microphone wake should still
    # work when the safety module is unavailable.
    zoro_safety = None

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_sounddevice():
    """Load the optional microphone backend without a hard static import."""
    try:
        return importlib.import_module("sounddevice")
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice is required for microphone wake detection; "
            "install it with the active Python environment"
        ) from exc


def _load_wakeword_model():
    """Load openWakeWord without requiring the type checker to resolve it."""
    try:
        return importlib.import_module("openwakeword.model").Model
    except ImportError as exc:
        raise RuntimeError(
            "openwakeword is required for wake detection; "
            "install it with the active Python environment"
        ) from exc


# --- Windows CoreAudio output peak meter (speaker/media bleed guard) --------


class _GUID(Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, l, w1, w2, b1, b2, b3, b4, b5, b6, b7, b8):
        super().__init__(
            l, w1, w2, (ctypes.c_ubyte * 8)(b1, b2, b3, b4, b5, b6, b7, b8)
        )


_CLSID_MMDeviceEnumerator = _GUID(
    0xBCDE0395, 0xE52F, 0x467C, 0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E
)
_IID_IMMDeviceEnumerator = _GUID(
    0xA95664D2, 0x9614, 0x4F35, 0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6
)
_IID_IAudioMeterInformation = _GUID(
    0xC02216F6, 0x8C67, 0x4B5B, 0x9D, 0x00, 0xD0, 0x08, 0xE7, 0x3E, 0x00, 0x64
)


def _com_release(ptr: c_void_p | None) -> None:
    if ptr and ptr.value:
        try:
            vt = ctypes.cast(
                ctypes.cast(ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            release_fn = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vt[2])
            release_fn(ptr)
        except Exception:
            pass
        ptr.value = None


class _SystemAudioMeter:
    """Reads the Windows master render endpoint peak meter via CoreAudio COM.

    Allows the wake service to know when the PC is playing audio (movies,
    music, YouTube, games, or Zoro's own TTS) so speaker bleed into the
    microphone never false-triggers the wake word.
    """

    def __init__(self) -> None:
        self._available = sys.platform == "win32"
        self._enum_ptr: c_void_p | None = None
        self._dev_ptr: c_void_p | None = None
        self._meter_ptr: c_void_p | None = None
        self._get_peak_fn: Any = None
        self._last_bind = 0.0

    def _bind_endpoint(self) -> bool:
        if not self._available:
            return False
        try:
            ole32 = ctypes.oledll.ole32
            if self._enum_ptr is None or not self._enum_ptr.value:
                ole32.CoInitialize(None)
                enum_ptr = c_void_p()
                hr = ole32.CoCreateInstance(
                    byref(_CLSID_MMDeviceEnumerator),
                    None,
                    7,  # CLSCTX_ALL
                    byref(_IID_IMMDeviceEnumerator),
                    byref(enum_ptr),
                )
                if hr != 0 or not enum_ptr.value:
                    return False
                self._enum_ptr = enum_ptr

            _com_release(self._meter_ptr)
            _com_release(self._dev_ptr)
            self._get_peak_fn = None

            vt = ctypes.cast(
                ctypes.cast(self._enum_ptr, POINTER(c_void_p))[0],
                POINTER(c_void_p),
            )
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender=0, eMultimedia=1)
            get_endpoint = ctypes.WINFUNCTYPE(
                ctypes.c_long, c_void_p, c_uint, c_uint, POINTER(c_void_p)
            )(vt[4])
            dev_ptr = c_void_p()
            if get_endpoint(self._enum_ptr, 0, 1, byref(dev_ptr)) != 0 or not dev_ptr.value:
                return False
            self._dev_ptr = dev_ptr

            dev_vt = ctypes.cast(
                ctypes.cast(dev_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            # IMMDevice::Activate(iid, dwClsCtx, pActivationParams, ppInterface)
            activate = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                c_void_p,
                POINTER(_GUID),
                c_ulong,
                c_void_p,
                POINTER(c_void_p),
            )(dev_vt[3])
            meter_ptr = c_void_p()
            if (
                activate(
                    dev_ptr,
                    byref(_IID_IAudioMeterInformation),
                    7,
                    None,
                    byref(meter_ptr),
                )
                != 0
                or not meter_ptr.value
            ):
                _com_release(self._dev_ptr)
                return False
            self._meter_ptr = meter_ptr

            meter_vt = ctypes.cast(
                ctypes.cast(meter_ptr, POINTER(c_void_p))[0], POINTER(c_void_p)
            )
            # IAudioMeterInformation::GetPeakValue(&pfPeak)
            self._get_peak_fn = ctypes.WINFUNCTYPE(
                ctypes.c_long, c_void_p, POINTER(c_float)
            )(meter_vt[3])
            self._last_bind = time.monotonic()
            return True
        except Exception:
            return False

    def peak(self) -> float:
        if not self._available:
            return 0.0
        now = time.monotonic()
        if self._get_peak_fn is None or (now - self._last_bind) > 5.0:
            if not self._bind_endpoint():
                return 0.0
        try:
            val = c_float(0.0)
            get_peak_fn = self._get_peak_fn
            if get_peak_fn is not None and get_peak_fn(self._meter_ptr, byref(val)) == 0:
                return float(val.value)
            # Endpoint may have changed (headphones plugged/unplugged); rebind next call.
            self._get_peak_fn = None
        except Exception:
            self._get_peak_fn = None
        return 0.0


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
    """Surface the Zoro desktop app on wake and trigger a session when no
    WebSocket client is already connected. Skipped by default when a webview
    client is already connected over ws://127.0.0.1:8765 so we don't double-
    trigger or steal focus from fullscreen applications.
    """
    if clients and os.environ.get("ZORO_ALWAYS_SUMMON_APP", "0") != "1":
        return
    if os.environ.get("ZORO_AUTO_SUMMON_APP", "0") != "1":
        return
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
# Higher default threshold (0.78) + Silero VAD (0.65) + multi-frame patience (2)
# prevents false triggers from casual conversation, movie dialogue, or music.
THRESHOLD = float(os.environ.get("ZORO_WAKE_THRESHOLD", "0.78"))
VAD_THRESHOLD = float(os.environ.get("ZORO_WAKE_VAD_THRESHOLD", "0.65"))
PATIENCE_FRAMES = int(os.environ.get("ZORO_WAKE_PATIENCE", "2"))
MIN_MIC_RMS = float(os.environ.get("ZORO_WAKE_MIN_RMS", "250.0"))
COOLDOWN_S = 5.0            # ignore repeat detections within this window

# Suppress wake-word detection while PC speakers are playing media (movies,
# music, YouTube, etc.) + a short holdover tail after sound stops so speaker
# bleed and room echo never wake Zoro uncalled.
SUPPRESS_DURING_MEDIA = os.environ.get("ZORO_WAKE_DURING_MEDIA", "0") != "1"
MEDIA_PEAK_THRESHOLD = float(os.environ.get("ZORO_MEDIA_PEAK_THRESHOLD", "0.005"))
MEDIA_HOLDOVER_S = float(os.environ.get("ZORO_MEDIA_HOLDOVER_S", "2.5"))

HOST = "127.0.0.1"
PORT = 8765

SAMPLE_RATE = 16000
FRAME = 1280                # 80 ms @ 16 kHz — openWakeWord's expected chunk

# A call-pause is a *lease*, not a latch: the browser refreshes it every few
# seconds while a call is live, and it auto-expires PAUSE_TTL_S after the last
# refresh. Must comfortably exceed the browser's refresh interval (~4 s).
PAUSE_TTL_S = 10.0
POST_CALL_COOLDOWN_S = 3.5
PROACTIVE_COOLDOWN_S = 30.0

clients: set = set()
# Deadline (time.monotonic) until which the call-pause lease mutes the mic; 0
# means no active lease. The tray's "Pause listening" is a *separate*, sticky
# mute (no TTL) so the user can intentionally silence Zoro indefinitely.
_pause_until = 0.0
_muted_sticky = False
_pause_logged = False      # so per-refresh leases don't spam the log
_media_active_until = 0.0
_media_logged = False
_last_proactive_fire = 0.0


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
        # Keep a brief post-call cooldown so residual TTS audio or room echo
        # after hangup cannot immediately bounce the session back on.
        _pause_until = time.monotonic() + POST_CALL_COOLDOWN_S
        if _pause_logged:
            print("[wake] resumed (call ended, post-call cooldown active)", flush=True)
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


def _is_media_playing() -> bool:
    return SUPPRESS_DURING_MEDIA and (time.monotonic() < _media_active_until)


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
                # Tray "Pause listening" or voice "deactivate": sticky, indefinite.
                global _muted_sticky
                _muted_sticky = True
                print("[wake] muted (sticky)", flush=True)
            elif cmd == "unmute":
                _muted_sticky = False
                print("[wake] unmuted (sticky)", flush=True)
            elif cmd == "proactive":
                # The proactive service asks us to wake the browser so it
                # connects and the agent delivers the queued directive.
                _broadcast_proactive(asyncio.get_running_loop(), source_ws=ws)
    except ConnectionClosed:
        # The hosted UI's ws:// to loopback frequently drops without a close
        # handshake; that's expected, not an error.
        pass
    finally:
        clients.discard(ws)
        print(f"[wake] client disconnected ({len(clients)} total)")


def _broadcast_proactive(loop, source_ws=None):
    """Notify connected web clients to connect to Zoro for a proactive directive,
    unless muted, stood down, in quiet hours, or watching media."""
    global _last_proactive_fire
    now = time.monotonic()
    if (now - _last_proactive_fire) < PROACTIVE_COOLDOWN_S:
        return
    if _is_muted():
        print("[wake] proactive trigger suppressed (muted / call active)", flush=True)
        return
    if _is_media_playing():
        print("[wake] proactive trigger suppressed (media playing)", flush=True)
        return
    if zoro_safety is not None:
        try:
            ok, reason = zoro_safety.can_interrupt()
            if not ok:
                print(f"[wake] proactive trigger suppressed ({reason})", flush=True)
                return
        except Exception:
            pass

    _last_proactive_fire = now
    _set_call_pause(True)
    print("[wake] proactive trigger -> notifying browser", flush=True)
    _summon_app("--proactive")
    msg = json.dumps({"event": "proactive"})
    for ws in list(clients):
        if ws is not source_ws:
            loop.create_task(ws.send(msg))


def _broadcast_wake(loop, name):
    # Immediately start the call-pause lease so we don't fire a second time
    # while the frontend is fetching its LiveKit token.
    _set_call_pause(True)
    _summon_app("--wake")
    msg = json.dumps({"event": "wake", "model": name})
    for ws in list(clients):
        loop.create_task(ws.send(msg))


def _redis_proactive_loop(loop):
    """Listen for proactive events emitted via Azure Redis across distributed workers."""
    try:
        if zoro_safety is None:
            return
        r = zoro_safety._get_redis()
        if not r:
            return
        pubsub = r.pubsub()
        pubsub.subscribe("zoro:proactive:events")
        print("[wake] subscribed to Azure Redis zoro:proactive:events", flush=True)
        for message in pubsub.listen():
            if message and message.get("type") == "message":
                print("[wake] received proactive event from Azure Redis", flush=True)
                loop.call_soon_threadsafe(_broadcast_proactive, loop)
    except Exception as e:
        print(f"[wake] redis proactive subscriber ended: {e}", flush=True)


def _audio_loop(loop):
    global _media_active_until, _media_logged
    sd = _load_sounddevice()
    model_class = _load_wakeword_model()
    try:
        model = model_class(
            wakeword_models=[WAKE_MODEL],
            vad_threshold=VAD_THRESHOLD,
            inference_framework="onnx",
        )
    except Exception:
        model = model_class(wakeword_models=[WAKE_MODEL], inference_framework="onnx")

    audio_meter = _SystemAudioMeter()
    last_fire = 0.0
    consecutive_hits = 0
    was_suppressed = False
    rms_window: deque[float] = deque(maxlen=6)

    print(
        f"[wake] listening for '{WAKE_MODEL}' "
        f"(threshold={THRESHOLD}, vad={VAD_THRESHOLD}, patience={PATIENCE_FRAMES}, "
        f"media_guard={SUPPRESS_DURING_MEDIA})",
        flush=True,
    )
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME
    ) as stream:
        while True:
            data, _ = stream.read(FRAME)
            now_mono = time.monotonic()

            # Check whether PC speakers are currently playing media (movie/music/TTS)
            if SUPPRESS_DURING_MEDIA:
                sys_peak = audio_meter.peak()
                if sys_peak >= MEDIA_PEAK_THRESHOLD:
                    _media_active_until = now_mono + MEDIA_HOLDOVER_S
                    if not _media_logged:
                        print(
                            f"[wake] media playback active (peak={sys_peak:.3f}) — "
                            "suppressing mic wake",
                            flush=True,
                        )
                        _media_logged = True
                elif _media_logged and now_mono >= _media_active_until:
                    print("[wake] media playback ended — mic wake armed", flush=True)
                    _media_logged = False

            if _is_muted() or _is_media_playing():
                if not was_suppressed:
                    model.reset()
                    consecutive_hits = 0
                    rms_window.clear()
                    was_suppressed = True
                continue

            if was_suppressed:
                model.reset()
                consecutive_hits = 0
                rms_window.clear()
                was_suppressed = False

            samples = np.squeeze(data)
            frame_rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            rms_window.append(frame_rms)
            peak_rms = max(rms_window) if rms_window else frame_rms

            scores = model.predict(samples)
            name, score = max(scores.items(), key=lambda kv: kv[1])

            if score >= THRESHOLD and peak_rms >= MIN_MIC_RMS:
                consecutive_hits += 1
            else:
                consecutive_hits = 0

            now = time.time()
            if consecutive_hits >= PATIENCE_FRAMES and (now - last_fire) > COOLDOWN_S:
                last_fire = now
                consecutive_hits = 0
                rms_window.clear()
                model.reset()
                print(
                    f"[wake] DETECTED {name} ({score:.2f}, rms={peak_rms:.0f}) -> notifying browser",
                    flush=True,
                )
                loop.call_soon_threadsafe(_broadcast_wake, loop, name)


async def main():
    loop = asyncio.get_running_loop()
    threading.Thread(target=_audio_loop, args=(loop,), daemon=True).start()
    threading.Thread(target=_redis_proactive_loop, args=(loop,), daemon=True).start()
    async with websockets.serve(_handler, HOST, PORT):
        print(f"[wake] WebSocket up on ws://{HOST}:{PORT}", flush=True)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[wake] stopped")
