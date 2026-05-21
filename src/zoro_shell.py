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


def run_powershell(command: str, timeout: int = 180) -> dict:
    """Run a PowerShell command. Returns {stdout, stderr, exit_code}.

    Raises subprocess.TimeoutExpired if it hangs past `timeout`.
    """
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
    return next(
        (a for a in apps if needle in a["name"].lower()), None
    )


def open_app(name: str) -> dict:
    """Launch an installed Windows app by fuzzy name.

    Returns the matched {name, app_id}. Raises LookupError if no
    match.
    """
    match = find_app(name)
    if not match:
        raise LookupError(f"no installed app matching {name!r}")
    # shell:AppsFolder works for Win32, MSIX, and UWP apps alike.
    # Backticks escape the backslash inside the double-quoted PS string.
    app_id = match["app_id"].replace('"', '`"')
    cmd = f'Start-Process "shell:AppsFolder\\{app_id}"'
    res = run_powershell(cmd, timeout=10)
    if res["exit_code"] != 0:
        raise RuntimeError(
            (res["stderr"] or "Start-Process failed").strip()
        )
    return match
