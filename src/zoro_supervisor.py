"""Zoro always-on supervisor.

Spawns and babysits the three Zoro processes so the whole assistant behaves
like a single piece of infrastructure rather than three terminals:

  - agent worker   .venv python src/zoro_agent.py dev
  - wake service   .venv python src/wake_service.py
  - web frontend   npm run dev   (in ./web)

It restarts any process that dies (with crash-loop backoff), funnels each
one's output to data/<name>.log with UTF-8/unbuffered env baked in (the
encoding fix we otherwise had to set by hand), and shows a system-tray icon
to pause/resume listening, toggle stand-down, restart, or quit.

Run:
    .venv/Scripts/python.exe src/zoro_supervisor.py            (with tray)
    .venv/Scripts/python.exe src/zoro_supervisor.py --no-tray  (headless)

Started automatically at log-on by scripts/install_autostart.ps1.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / "data"
WEB = ROOT / "web"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CHILD_ENV = {
    **os.environ,
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    # Never write or trust bytecode cache — always run from source. This
    # project has historically lived in cloud-synced folders (e.g.
    # OneDrive) where sync rewrites file mtimes, so Python loaded stale
    # __pycache__/*.pyc instead of edited source and changes silently
    # didn't take effect. Cheap insurance to keep even outside such folders.
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _npm() -> str:
    return shutil.which("npm") or shutil.which("npm.cmd") or "npm.cmd"


def _node() -> str:
    return shutil.which("node") or shutil.which("node.exe") or "node"


WHATSAPP = ROOT / "services" / "whatsapp"


# name -> (argv, cwd)
def _services() -> dict[str, tuple[list[str], Path]]:
    return {
        # `start`, NOT `dev`: dev mode runs a file-watcher that forks a
        # multiprocessing worker child. Those children outlived name-based
        # kills and kept registering to LiveKit as zombie workers running
        # stale code, stealing sessions. `start` is one clean process the
        # supervisor fully owns and can restart deterministically.
        "agent": ([str(PY), "src/zoro_agent.py", "start"], ROOT),
        "wake": ([str(PY), "src/wake_service.py"], ROOT),
        "proactive": ([str(PY), "src/zoro_proactive.py"], ROOT),
        "reflector": ([str(PY), "src/zoro_reflector.py"], ROOT),
        # NOTE: the local Next dev server (localhost:3000) is intentionally
        # NOT managed anymore. The desktop app loads the hosted UI from
        # Vercel, and a local browser tab on :3000 was connecting to the wake
        # service and pausing it (the Tauri app can't send resume over the
        # blocked ws://). Run `cd web && npm run dev` by hand if you ever need
        # to debug the UI locally in a browser.
        # WhatsApp bridge: drives WhatsApp Web via Puppeteer, exposes a
        # local HTTP API. First run needs an interactive QR scan from a
        # real terminal — until session is cached at
        # data/whatsapp-session/, this will sit in `qr` status and the
        # agent's check_whatsapp tool will report "not connected".
        "whatsapp": ([_node(), "bridge.js"], WHATSAPP),
    }


class Supervisor:
    """Owns the child processes and the restart loop."""

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, object] = {}
        self._restarts: dict[str, list[float]] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # --- process lifecycle -------------------------------------------------

    def _spawn(self, name: str) -> None:
        argv, cwd = _services()[name]
        log = self._logs.get(name)
        if log is None:
            DATA.mkdir(parents=True, exist_ok=True)
            log = open(DATA / f"{name}.log", "w", encoding="utf-8", buffering=1)
            self._logs[name] = log
        log.write(f"\n===== {name} starting {time.strftime('%H:%M:%S')} =====\n")
        log.flush()
        self._procs[name] = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=_CHILD_ENV,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )

    def _should_backoff(self, name: str) -> bool:
        """True if `name` has crash-looped (>5 restarts in 60s)."""
        now = time.time()
        hist = [t for t in self._restarts.get(name, []) if now - t < 60]
        hist.append(now)
        self._restarts[name] = hist
        return len(hist) > 5

    def start_all(self) -> None:
        with self._lock:
            for name in _services():
                self._spawn(name)

    def restart_all(self) -> None:
        self.stop_all(keep_logs=True)
        self._stop.clear()
        self.start_all()
        threading.Thread(target=self._watch, daemon=True).start()

    @staticmethod
    def _tree_kill(proc: subprocess.Popen) -> None:
        """Kill a child AND every descendant. Plain terminate() leaves
        grandchildren (e.g. anything that forks a worker) orphaned and
        still bound to ports / registered to LiveKit."""
        if proc.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=_CREATE_NO_WINDOW,
                timeout=10,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def stop_all(self, keep_logs: bool = False) -> None:
        self._stop.set()
        with self._lock:
            for _name, proc in list(self._procs.items()):
                self._tree_kill(proc)
            for proc in self._procs.values():
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    pass
            self._procs.clear()
            if not keep_logs:
                for log in self._logs.values():
                    try:
                        log.close()  # type: ignore[attr-defined]
                    except OSError:
                        pass
                self._logs.clear()

    def _watch(self) -> None:
        """Poll children; restart any that exit while we're meant to be up."""
        while not self._stop.is_set():
            time.sleep(2)
            if self._stop.is_set():
                break
            with self._lock:
                for name, proc in list(self._procs.items()):
                    if proc.poll() is None:
                        continue
                    log = self._logs.get(name)
                    if log is not None:
                        log.write(  # type: ignore[attr-defined]
                            f"\n[supervisor] {name} exited (code "
                            f"{proc.returncode}); restarting\n"
                        )
                        log.flush()  # type: ignore[attr-defined]
                    if self._should_backoff(name):
                        if log is not None:
                            log.write(  # type: ignore[attr-defined]
                                "[supervisor] crash-looping — backing off 30s\n"
                            )
                            log.flush()  # type: ignore[attr-defined]
                        time.sleep(30)
                    self._spawn(name)

    def run_forever(self) -> None:
        self.start_all()
        self._watch()

    def status(self) -> dict[str, bool]:
        with self._lock:
            return {
                n: (p.poll() is None) for n, p in self._procs.items()
            }


# --- wake-service control (pause/resume listening) -------------------------


def _wake_send(cmd: str) -> None:
    """Best-effort: tell the wake service to pause/resume the mic."""
    try:
        from websockets.sync.client import connect

        with connect("ws://127.0.0.1:8765", open_timeout=3) as ws:
            ws.send(f'{{"cmd": "{cmd}"}}')
    except Exception:  # noqa: BLE001 - tray control is best-effort
        pass


# --- system tray -----------------------------------------------------------


def _make_icon_image(ok: bool):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    core = (255, 182, 72) if ok else (120, 120, 120)
    d.ellipse((8, 8, 56, 56), fill=core)
    d.ellipse((24, 24, 40, 40), fill=(10, 14, 20))
    return img


def _run_tray(sup: Supervisor) -> None:
    import pystray
    from pystray import MenuItem as Item

    import zoro_safety

    def _quit(icon, _item):
        sup.stop_all()
        icon.stop()

    def _restart(icon, _item):
        threading.Thread(target=sup.restart_all, daemon=True).start()

    def _toggle_standdown(icon, _item):
        zoro_safety.set_stand_down(not zoro_safety.is_stood_down())
        icon.update_menu()

    menu = pystray.Menu(
        Item(
            lambda _i: "Zoro: "
            + (
                "running"
                if all(sup.status().values()) and sup.status()
                else "degraded"
            ),
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        Item("Open Zoro UI", lambda: webbrowser.open("http://localhost:3000")),
        # "mute"/"unmute" are the sticky, indefinite tray controls — distinct
        # from the browser's auto-expiring call-pause lease ("pause"/"resume"),
        # so a tray mute stays muted until the user lifts it.
        Item("Pause listening", lambda: _wake_send("mute")),
        Item("Resume listening", lambda: _wake_send("unmute")),
        Item(
            "Stand down (no proactivity)",
            _toggle_standdown,
            checked=lambda _i: zoro_safety.is_stood_down(),
        ),
        pystray.Menu.SEPARATOR,
        Item("Restart all", _restart),
        Item("Quit Zoro", _quit),
    )

    icon = pystray.Icon("zoro", _make_icon_image(True), "Zoro", menu)

    def _refresh():
        while True:
            time.sleep(5)
            try:
                st = sup.status()
                icon.icon = _make_icon_image(bool(st) and all(st.values()))
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_refresh, daemon=True).start()
    icon.run()


_SINGLETON_PORT = 8799

# Hour (local, 0-23) for the nightly self-update check. Kept low-traffic so
# the brief child restart after an update lands when Zoro is least likely
# to be mid-conversation.
_UPDATE_HOUR = int(os.environ.get("ZORO_UPDATE_HOUR", "4"))


def _run_auto_update(reason: str) -> str:
    """Pull + boot-check the backend before (re)launching children. Returns
    the status string; never raises (updates must not block startup)."""
    try:
        import zoro_updater

        result = zoro_updater.auto_update()
        status = result.get("status", "unknown")
        print(f"[supervisor] auto-update ({reason}): {result}", flush=True)
        return status
    except Exception as e:  # noqa: BLE001 - an update failure must not stop Zoro
        print(f"[supervisor] auto-update error ({reason}): {e}", flush=True)
        return "error"


def _nightly_updater(sup: "Supervisor") -> None:
    """Once a day at _UPDATE_HOUR, pull origin/main; if new code landed and
    passed its boot-check, restart the children onto it. Daemon thread."""
    while True:
        now = time.localtime()
        secs = (
            (_UPDATE_HOUR - now.tm_hour) % 24 * 3600
            - now.tm_min * 60
            - now.tm_sec
        )
        if secs <= 0:
            secs += 86400
        time.sleep(secs)
        status = _run_auto_update("nightly")
        if status == "updated":
            print("[supervisor] new code adopted — restarting children",
                  flush=True)
            try:
                sup.restart_all()
            except Exception as e:  # noqa: BLE001
                print(f"[supervisor] post-update restart failed: {e}",
                      flush=True)
        # Guard against a fast loop if the clock math ever lands us early.
        time.sleep(3600)


def _acquire_singleton():
    """Bind a localhost port as a single-instance lock. If another
    supervisor already holds it, exit — duplicate supervisors were
    spawning duplicate agents that all registered to LiveKit and stole
    each other's sessions."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
    except OSError:
        print("[supervisor] another instance is already running — exiting",
              flush=True)
        raise SystemExit(0)
    return s  # keep the socket alive for the process lifetime


def main() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    _lock = _acquire_singleton()  # noqa: F841 - held for process lifetime
    parser = argparse.ArgumentParser(prog="zoro-supervisor")
    parser.add_argument(
        "--no-tray", action="store_true", help="run headless (no tray icon)"
    )
    args = parser.parse_args()

    # Pull the latest backend BEFORE launching children, so a relaunch
    # always runs the newest known-good code. Safe by construction: skips on
    # a dirty/diverged tree and rolls back any commit that fails boot-check.
    _run_auto_update("startup")

    sup = Supervisor()

    if args.no_tray:
        def _sig(_s, _f):
            sup.stop_all()
            sys.exit(0)

        signal.signal(signal.SIGINT, _sig)
        try:
            signal.signal(signal.SIGTERM, _sig)
        except (ValueError, OSError):
            pass
        threading.Thread(
            target=_nightly_updater, args=(sup,), daemon=True
        ).start()
        sup.run_forever()
        return

    # tray mode: supervisor loop in a daemon thread, tray on main thread
    sup.start_all()
    threading.Thread(target=sup._watch, daemon=True).start()
    threading.Thread(
        target=_nightly_updater, args=(sup,), daemon=True
    ).start()
    try:
        _run_tray(sup)
    finally:
        sup.stop_all()


if __name__ == "__main__":
    main()
