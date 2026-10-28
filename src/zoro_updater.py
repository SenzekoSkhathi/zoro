"""Safe self-update for Zoro's Python backend.

The backend isn't hosted — it runs on the laptop — so updates are *pulled*
from GitHub rather than served. The supervisor calls auto_update() at
startup (and nightly) so "push to origin/main" becomes "the laptop runs
the new code on next relaunch", without anyone typing git commands.

Safety contract (this code can crash-loop the whole assistant if it adopts
a broken commit, which has bitten this project before):

  1. Refuse on a dirty working tree — never clobber in-progress edits.
  2. Only fast-forward. If local main has diverged (e.g. local self-mod
     commits not yet pushed), skip and leave it for a human — never force.
  3. Boot-check the pulled code (`compileall src/`) BEFORE adopting it.
  4. If the boot-check fails, hard-reset back to the exact commit we were
     on, so the supervisor always launches known-good code.

Returns a small status dict so the supervisor can log what happened.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / "data"
_LOG = DATA / "update.log"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_REMOTE = "origin"
_BRANCH = "main"


def _git_bin() -> str:
    return shutil.which("git") or shutil.which("git.exe") or "git"


def _log(msg: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(f"[updater] {msg}", flush=True)


def _git(*args: str, timeout: int = 60) -> tuple[int, str]:
    """Run a git command in the repo. Returns (returncode, combined output)."""
    try:
        res = subprocess.run(
            [_git_bin(), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return res.returncode, (res.stdout + res.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"{type(e).__name__}: {e}"


def _is_git_repo() -> bool:
    rc, out = _git("rev-parse", "--is-inside-work-tree")
    return rc == 0 and out.strip() == "true"


def _working_tree_clean() -> bool:
    rc, out = _git("status", "--porcelain")
    return rc == 0 and out.strip() == ""


def _head() -> str | None:
    rc, out = _git("rev-parse", "HEAD")
    return out.strip() if rc == 0 else None


def boot_check() -> tuple[bool, str]:
    """Compile every module under src/ — catches syntax/import-time breakage
    before the supervisor launches the children on it. Fast; mirrors the
    self-mod harness's check so the two stay consistent."""
    py = str(PY) if PY.exists() else "python"
    try:
        res = subprocess.run(
            [py, "-m", "compileall", "-q", "src"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_NO_WINDOW,
        )
        return res.returncode == 0, (res.stdout + res.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"


def auto_update() -> dict:
    """Pull origin/main if safe, boot-check, and roll back on failure.

    Status values:
      not_git    - not a git checkout, nothing to do
      offline    - couldn't reach the remote (fetch failed)
      dirty      - uncommitted local changes; skipped to avoid clobbering
      diverged   - local has commits not on origin; can't fast-forward
      current    - already up to date
      updated    - fast-forwarded and the new code passed the boot-check
      rolled_back- pulled code failed the boot-check; reverted to prior HEAD
    """
    if not _is_git_repo():
        return {"status": "not_git"}

    if not _working_tree_clean():
        _log("skip: working tree dirty (uncommitted changes)")
        return {"status": "dirty"}

    before = _head()

    rc, out = _git("fetch", _REMOTE, _BRANCH, timeout=90)
    if rc != 0:
        _log(f"fetch failed (offline?): {out[:200]}")
        return {"status": "offline", "detail": out[:200]}

    # How far behind are we? 0 = up to date.
    rc, behind = _git("rev-list", "--count", f"HEAD..{_REMOTE}/{_BRANCH}")
    if rc != 0:
        _log(f"rev-list failed: {behind[:200]}")
        return {"status": "offline", "detail": behind[:200]}
    if behind.strip() == "0":
        return {"status": "current", "head": before}

    # Are we also ahead (diverged)? If so a fast-forward is impossible —
    # don't risk a merge/force; leave it for a human.
    _rc, ahead = _git("rev-list", "--count", f"{_REMOTE}/{_BRANCH}..HEAD")
    if ahead.strip() not in ("0", ""):
        _log(f"diverged: {ahead.strip()} local commit(s) not on {_REMOTE}; "
             "skipping auto-update")
        return {"status": "diverged", "ahead": ahead.strip()}

    _log(f"{behind.strip()} new commit(s) on {_REMOTE}/{_BRANCH}; "
         "fast-forwarding")
    rc, out = _git("merge", "--ff-only", f"{_REMOTE}/{_BRANCH}")
    if rc != 0:
        _log(f"ff-only merge failed: {out[:200]}")
        return {"status": "diverged", "detail": out[:200]}

    ok, detail = boot_check()
    after = _head()
    if ok:
        _log(f"updated {before[:7] if before else '?'} -> "
             f"{after[:7] if after else '?'}; boot-check passed")
        return {"status": "updated", "from": before, "to": after}

    # New code is broken — revert to exactly where we were.
    _log(f"boot-check FAILED on {after[:7] if after else '?'}: {detail[:200]}")
    if before:
        rc, rout = _git("reset", "--hard", before)
        if rc == 0:
            _log(f"rolled back to {before[:7]}")
            return {"status": "rolled_back", "to": before, "detail": detail[:200]}
        _log(f"ROLLBACK FAILED: {rout[:200]} — manual fix needed")
        return {"status": "rolled_back", "rollback_failed": True}
    return {"status": "rolled_back", "rollback_failed": True}


if __name__ == "__main__":
    import json

    print(json.dumps(auto_update(), indent=2))
