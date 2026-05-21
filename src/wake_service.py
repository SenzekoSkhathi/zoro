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
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets
from openwakeword.model import Model

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

clients: set = set()
paused = threading.Event()


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
                paused.set()
                print("[wake] paused (call active)")
            elif cmd == "resume":
                paused.clear()
                print("[wake] resumed (listening)")
            elif cmd == "proactive":
                # The proactive service asks us to wake the browser exactly
                # as a spoken wake word would, so it connects and the agent
                # delivers the queued directive.
                print("[wake] proactive trigger -> notifying browser")
                msg = json.dumps({"event": "proactive"})
                for c in list(clients):
                    if c is not ws:
                        await c.send(msg)
    finally:
        clients.discard(ws)
        print(f"[wake] client disconnected ({len(clients)} total)")


def _broadcast_wake(loop, name):
    # Scheduled onto the loop via call_soon_threadsafe, so this already
    # runs IN the loop thread. run_coroutine_threadsafe is the cross-thread
    # primitive and is the wrong tool here (it only worked by accident
    # because the future was never awaited); create_task is correct.
    msg = json.dumps({"event": "wake", "model": name})
    for ws in list(clients):
        loop.create_task(ws.send(msg))


def _audio_loop(loop):
    model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    last_fire = 0.0
    print(f"[wake] listening for '{WAKE_MODEL}' (threshold {THRESHOLD})")
    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME
    ) as stream:
        while True:
            data, _ = stream.read(FRAME)
            if paused.is_set():
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
    async with websockets.serve(_handler, HOST, PORT):
        print(f"[wake] WebSocket up on ws://{HOST}:{PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[wake] stopped")
