"""Generate src/SELF_MAP.md — Zoro's index of her own source code.

Walks src/ and the other top-level directories, pulls the first line of
each module's docstring, and writes a compact markdown index that
zoro_prompt.py injects into the system prompt at startup. Re-run after
any structural change (zoro_claude_code does this automatically after a
successful self-modification).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = SRC / "SELF_MAP.md"


def _summary(py_path: Path) -> str:
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return "(unparseable)"
    doc = ast.get_docstring(tree) or ""
    first = doc.strip().splitlines()[0] if doc.strip() else ""
    return first or "(no docstring)"


def _section_src() -> list[str]:
    lines = ["## Source modules (src/)"]
    for py in sorted(SRC.glob("zoro_*.py")):
        lines.append(f"- `src/{py.name}` — {_summary(py)}")
    wake = SRC / "wake_service.py"
    if wake.exists():
        lines.append(f"- `src/wake_service.py` — {_summary(wake)}")
    return lines


def _section_scripts() -> list[str]:
    scripts = ROOT / "scripts"
    if not scripts.exists():
        return []
    lines = ["", "## Scripts (scripts/)"]
    for p in sorted(scripts.iterdir()):
        if p.is_file():
            lines.append(f"- `scripts/{p.name}`")
    return lines


def _section_agents() -> list[str]:
    agents_dir = ROOT / "agents"
    if not agents_dir.exists():
        return []
    lines = ["", "## Sub-agents (agents/)"]
    for p in sorted(agents_dir.glob("*.md")):
        body = p.read_text(encoding="utf-8", errors="replace").splitlines()
        title = ""
        desc = ""
        for line in body:
            s = line.strip()
            if not title and s.startswith("#"):
                title = s.lstrip("# ").strip()
                continue
            if title and not desc and s:
                desc = s
                break
        lines.append(f"- `agents/{p.name}` — {title}: {desc}")
    return lines


def _section_other() -> list[str]:
    return [
        "",
        "## Other top-level directories",
        "- `services/whatsapp/` — Node bridge that connects WhatsApp Web to Zoro",
        "- `web/` — Next.js dashboard / HUD that Zoro publishes display state to",
        "- `data/` — runtime state (logs, notes, config, reminders) — gitignored",
        "- `models/` — local model files (wake word, etc.) — gitignored",
    ]


def build() -> str:
    header = [
        "# Zoro self-map",
        "",
        "This is your own source code. You are a Python voice assistant",
        "whose code lives at `c:/Users/senze/Downloads/zoro`. When the user",
        "asks you to change, improve, or extend yourself, use the module",
        "list below to know which file to point Claude Code at via the",
        "`ask_claude_code` tool.",
        "",
    ]
    parts = (
        header
        + _section_src()
        + _section_agents()
        + _section_scripts()
        + _section_other()
        + [""]
    )
    return "\n".join(parts)


def main() -> None:
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
