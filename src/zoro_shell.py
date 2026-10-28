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


def list_installed_apps() -> list[dict]:
    """Enumerate Start-Menu apps via Get-StartApps.

    Returns [{name, app_id}]. Covers Win32, MSIX/UWP, and shortcut-
    installed apps — anything Windows considers an "app" you can
    launch from Start.
    """
    res = run_powershell("Get-StartApps | ConvertTo-Json -Compress")
    if res["exit_code"] != 0:
        raise RuntimeError(
            (res["stderr"] or "Get-StartApps failed").strip()
        )
    raw = (res["stdout"] or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    return [
        {"name": d.get("Name", ""), "app_id": d.get("AppID", "")}
        for d in data
    ]


def find_app(name: str) -> Optional[dict]:
    """Best fuzzy match for an installed app. Case-insensitive."""
    needle = name.lower().strip()
    if not needle:
        return None
    apps = list_installed_apps()
    exact = next(
        (a for a in apps if a["name"].lower() == needle), None
    )
    if exact:
        return exact
    prefix = next(
        (a for a in apps if a["name"].lower().startswith(needle)),
        None,
    )
    if prefix:
        return prefix
    substring = next(
        (a for a in apps if needle in a["name"].lower()), None
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
            SequenceMatcher(None, needle, title).ratio(),
            SequenceMatcher(None, needle, title.split()[0] if title.split() else title).ratio(),
        )
        if score > best_score:
            best_score, best = score, a
    # 0.8 keeps "amatuba"~"amathuba" (~0.93) while rejecting unrelated names.
    return best if best_score >= 0.8 else None


def _local_open_app(name: str) -> dict:
    match = find_app(name)
    if not match:
        raise LookupError(f"no installed app matching {name!r}")
    app_id = match["app_id"].replace('"', '`"')
    cmd = f'Start-Process "shell:AppsFolder\\{app_id}"'
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
