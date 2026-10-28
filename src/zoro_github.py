"""GitHub access via the gh CLI.

Mirror of zoro_claude_code's pattern: shell out to a well-tested tool
(`gh`), parse its --json output, return structured dicts. Auth lives
in gh's own credential store (~/.config/gh on POSIX, %APPDATA%\\GitHub
CLI\\ on Windows), populated by `gh auth login`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Optional


_GH_FALLBACKS = (
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
)


def _gh_bin() -> str:
    found = shutil.which("gh")
    if found:
        return found
    # PATH may be stale (gh installed after the agent process started).
    # Fall back to the standard winget/MSI install locations.
    import os

    for candidate in _GH_FALLBACKS:
        if os.path.exists(candidate):
            return candidate
    return "gh"


def _gh(*args: str, cwd: Optional[str] = None) -> str:
    """Run `gh <args>` and return stdout. Raises RuntimeError on failure."""
    res = subprocess.run(
        [_gh_bin(), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        encoding="utf-8",
        errors="replace",
    )
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip() or (
            f"gh exited with code {res.returncode}"
        )
        raise RuntimeError(msg)
    return res.stdout


def notifications(limit: int = 10) -> list[dict]:
    """Recent GitHub notifications across all repos.

    Returns dicts: {repo, reason, type, title, url, unread, updated_at}.
    URLs are converted from API form to browser form so the user can
    click straight through from the card.
    """
    raw = _gh("api", f"notifications?per_page={limit}")
    items = json.loads(raw)
    out: list[dict] = []
    for n in items:
        subject = n.get("subject", {})
        repo = (n.get("repository") or {}).get("full_name", "")
        url = subject.get("url") or ""
        url = url.replace(
            "api.github.com/repos", "github.com"
        ).replace("/pulls/", "/pull/")
        out.append(
            {
                "repo": repo,
                "reason": n.get("reason", ""),
                "type": subject.get("type", ""),
                "title": subject.get("title", ""),
                "url": url,
                "unread": n.get("unread", True),
                "updated_at": n.get("updated_at", ""),
            }
        )
    return out


def my_open_prs(limit: int = 20) -> list[dict]:
    """Open PRs authored by the authenticated user across all repos."""
    raw = _gh(
        "search",
        "prs",
        "--author",
        "@me",
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,title,url,repository,isDraft,createdAt",
    )
    items = json.loads(raw)
    out: list[dict] = []
    for p in items:
        out.append(
            {
                "number": p.get("number"),
                "title": p.get("title", ""),
                "url": p.get("url", ""),
                "repo": (p.get("repository") or {}).get("nameWithOwner", ""),
                "draft": p.get("isDraft", False),
                "created_at": p.get("createdAt", ""),
            }
        )
    return out


def pr_details(repo: str, number: int) -> dict:
    """Detailed view of one PR: state, checks, reviews, mergeable.

    repo: "owner/name"
    """
    raw = _gh(
        "pr",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        (
            "number,title,state,isDraft,statusCheckRollup,reviewDecision,"
            "mergeable,url,baseRefName,headRefName,author"
        ),
    )
    p = json.loads(raw)
    checks = p.get("statusCheckRollup") or []
    counts: dict[str, int] = {}
    for c in checks:
        conc = (
            c.get("conclusion") or c.get("state") or "PENDING"
        ).upper()
        counts[conc] = counts.get(conc, 0) + 1
    return {
        "number": p.get("number"),
        "title": p.get("title", ""),
        "state": p.get("state", ""),
        "draft": p.get("isDraft", False),
        "review_decision": p.get("reviewDecision") or "PENDING",
        "mergeable": p.get("mergeable") or "UNKNOWN",
        "url": p.get("url", ""),
        "base": p.get("baseRefName", ""),
        "head": p.get("headRefName", ""),
        "author": (p.get("author") or {}).get("login", ""),
        "checks_total": len(checks),
        "checks_counts": counts,
    }


def open_pr(
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str = "",
) -> str:
    """Open a PR. Returns the new PR's URL."""
    return _gh(
        "pr",
        "create",
        "--repo",
        repo,
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
    ).strip()
