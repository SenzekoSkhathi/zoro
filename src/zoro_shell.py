"""PowerShell access + Windows app launching.

Zoro's keys to the kingdom: run arbitrary PowerShell (installs,
system tweaks, anything `pwsh` can do) and launch any installed app
by fuzzy name.

This module just *executes*. The contract that the agent reads the
command back and waits for confirmation lives in the tool docstrings
in zoro_agent.py and is enforced by the LLM. Every command this
module runs is appended to `data/shell_audit.log` so the user can
review what Zoro actually did.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
_AUDIT = ROOT / "data" / "shell_audit.log"


def _audit(command: str, exit_code: int) -> None:
    try:
        _AUDIT.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with _AUDIT.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] exit={exit_code}  {command}\n")
    except OSError:
        pass  # auditing must never break the call


def _pwsh() -> str:
    # Prefer PowerShell 7+ (`pwsh`) if available, fall back to Windows
    # PowerShell 5.1 (`powershell`). Both accept the same -Command flag.
    return (
        shutil.which("pwsh")
        or shutil.which("powershell")
        or "powershell"
    )


def is_cloud() -> bool:
    """Return True if running in cloud (Azure Container Apps / Linux) or forced via env."""
    import os
    import sys

    if os.environ.get("ZORO_FORCE_RELAY") == "1":
        return True
    return os.environ.get("ZORO_CLOUD") == "1" or sys.platform != "win32"


def _local_run_powershell(command: str, timeout: int = 180) -> dict:
    res = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    _audit(command, res.returncode)
    return {
        "stdout": res.stdout or "",
        "stderr": res.stderr or "",
        "exit_code": res.returncode,
    }


def run_powershell(command: str, timeout: int = 180) -> dict:
    """Run a PowerShell command. Returns {stdout, stderr, exit_code}.

    Raises subprocess.TimeoutExpired if it hangs past `timeout`.
    """
    if is_cloud():
        try:
            import zoro_screen_relay
            return zoro_screen_relay.request_host_command(
                "powershell", {"command": command, "timeout": timeout}, timeout=float(timeout + 5)
            )
        except Exception as e:
            raise RuntimeError(
                f"Could not execute PowerShell on Windows host via relay: {e}. "
                "Ensure Zoro Host Relay or Zoro Supervisor is running on your Windows laptop."
            ) from e

    return _local_run_powershell(command, timeout)


# --- App launching ---------------------------------------------------------


# Common aliases so spoken variations and short names map cleanly to Start Menu titles
APP_NAME_ALIASES: dict[str, str] = {
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "code": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "calc": "Calculator",
    "calculator": "Calculator",
    "browser": "Microsoft Edge",
    "edge": "Microsoft Edge",
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "terminal": "Windows Terminal",
    "powershell": "Windows PowerShell",
    "cmd": "Command Prompt",
    "files": "File Explorer",
    "explorer": "File Explorer",
    "file explorer": "File Explorer",
    "word": "Word",
    "excel": "Excel",
    "spotify": "Spotify",
    "paint": "Paint",
    "notepad": "Notepad",
    "settings": "Settings",
    "camera": "Camera",
}

_APPS_CACHE: list[dict] = []
_APPS_CACHE_TIME: float = 0.0
_APPS_CACHE_TTL: float = 3600.0  # In-memory TTL
_APPS_CACHE_FILE = ROOT / "data" / "installed_apps.json"


def list_installed_apps(force_refresh: bool = False) -> list[dict]:
    """Enumerate Start-Menu apps via Get-StartApps with persistent disk & in-memory caching.

    Returns [{name, app_id}]. Covers Win32, MSIX/UWP, and shortcut-
    installed apps — anything Windows considers an "app" you can
    launch from Start.
    """
    global _APPS_CACHE, _APPS_CACHE_TIME
    now = time.time()
    if not force_refresh and _APPS_CACHE and (now - _APPS_CACHE_TIME < _APPS_CACHE_TTL):
        return _APPS_CACHE

    # Load from persistent disk cache if fresh (< 24 hours)
    if not force_refresh and _APPS_CACHE_FILE.exists():
        try:
            mtime = _APPS_CACHE_FILE.stat().st_mtime
            if now - mtime < 86400.0:  # 24 hours
                data = json.loads(_APPS_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    _APPS_CACHE = data
                    _APPS_CACHE_TIME = now
                    return _APPS_CACHE
        except Exception:
            pass

    res = run_powershell("Get-StartApps | ConvertTo-Json -Compress")
    if res["exit_code"] != 0:
        if _APPS_CACHE:
            return _APPS_CACHE
        raise RuntimeError(
            (res["stderr"] or "Get-StartApps failed").strip()
        )
    raw = (res["stdout"] or "").strip()
    if not raw:
        return _APPS_CACHE
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        apps = [
            {"name": d.get("Name", ""), "app_id": d.get("AppID", "")}
            for d in data
            if d.get("Name") and d.get("AppID")
        ]
        _APPS_CACHE = apps
        _APPS_CACHE_TIME = now
        try:
            _APPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _APPS_CACHE_FILE.write_text(json.dumps(apps, indent=2), encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass
    return _APPS_CACHE


def find_app(name: str) -> Optional[dict]:
    """Best fuzzy match for an installed app. Case-insensitive."""
    needle = name.lower().strip()
    if not needle:
        return None

    # Check aliases first
    alias_target = APP_NAME_ALIASES.get(needle)
    needle_resolved = alias_target.lower() if alias_target else needle

    apps = list_installed_apps()
    # 1. Exact match against resolved needle or original needle
    exact = next(
        (a for a in apps if a["name"].lower() in (needle_resolved, needle)), None
    )
    if exact:
        return exact

    # 2. Prefix match
    prefix = next(
        (a for a in apps if a["name"].lower().startswith(needle_resolved) or a["name"].lower().startswith(needle)),
        None,
    )
    if prefix:
        return prefix

    # 3. Substring match
    substring = next(
        (a for a in apps if needle_resolved in a["name"].lower() or needle in a["name"].lower()), None
    )
    if substring:
        return substring

    # Fuzzy fallback for speech-to-text drift (e.g. "amatuba" -> "amathuba",
    # "discrod" -> "discord"). A dropped/extra letter defeats the substring
    # check but barely dents similarity, so compare against each app name
    # (and its first word, so "amathuba" matches a longer official title)
    # and take the best above a conservative threshold.
    from difflib import SequenceMatcher

    best: Optional[dict] = None
    best_score = 0.0
    for a in apps:
        title = a["name"].lower()
        score = max(
            SequenceMatcher(None, needle_resolved, title).ratio(),
            SequenceMatcher(None, needle_resolved, title.split()[0] if title.split() else title).ratio(),
        )
        if score > best_score:
            best_score, best = score, a
    # 0.8 keeps "amatuba"~"amathuba" (~0.93) while rejecting unrelated names.
    return best if best_score >= 0.8 else None


def _local_open_app(name: str) -> dict:
    match = find_app(name)
    if not match:
        raise LookupError(f"no installed app matching {name!r}")
    app_id = match["app_id"]
    target = f"shell:AppsFolder\\{app_id}"

    # Fast-path: Win32 native ShellExecute via os.startfile (<500ms)
    import os
    if hasattr(os, "startfile"):
        try:
            os.startfile(target)
            return match
        except Exception:
            pass

    # Fallback to PowerShell Start-Process
    safe_id = app_id.replace('"', '`"')
    cmd = f'Start-Process "shell:AppsFolder\\{safe_id}"'
    res = _local_run_powershell(cmd, timeout=10)
    if res["exit_code"] != 0:
        raise RuntimeError(
            (res["stderr"] or "Start-Process failed").strip()
        )
    return match


def open_app(name: str) -> dict:
    """Launch an installed Windows app by fuzzy name.

    Returns the matched {name, app_id}. Raises LookupError if no
    match.
    """
    if is_cloud():
        try:
            import zoro_screen_relay
            return zoro_screen_relay.request_host_command("open_app", {"name": name}, timeout=15.0)
        except Exception as e:
            raise RuntimeError(
                f"Could not launch app '{name}' on Windows host via relay: {e}. "
                "Ensure Zoro Host Relay or Zoro Supervisor is running on your Windows laptop."
            ) from e

    return _local_open_app(name)
