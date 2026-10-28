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


def _section_web() -> list[str]:
    web_dir = ROOT / "web"
    if not web_dir.exists():
        return []
    return [
        "",
        "## Frontend web dashboard & HUD (web/)",
        "- `web/app/page.tsx` — Main HUD dashboard: LiveKit audio visualizer, Siri Orb, HUD panels, telemetry",
        "- `web/components/Orb.tsx` — Reactive multi-band 3D/canvas Siri Orb with audio reactivity",
        "- `web/components/ZoroDisplay.tsx` — Multi-widget HUD display cards (search, weather, calendar, inbox, spotify, github, youtube, etc.)",
        "- `web/components/WidgetLimit.tsx` — Shared widget limiter (1 big + 2 small max) and centre-screen WidgetStage grid",
        "- `web/components/InstrumentRail.tsx` — System telemetry & resource gauges (CPU, memory, latency)",
        "- `web/components/Transcript.tsx` — Real-time speech transcript & conversation history",
        "- `web/components/ToolTicker.tsx` — Active tool execution ticker and progress indicators",
        "- `web/components/HudPanel.tsx` — Collapsible HUD information drawer",
        "- `web/components/LoginGate.tsx` — Authentication & session gate for web HUD",
        "- `web/app/api/token/route.ts` — LiveKit WebRTC access token generation API",
        "- `web/app/api/session/route.ts` — Agent session dispatch & lifecycle control API",
        "- `web/app/api/telemetry/route.ts` — PostgreSQL / App Insights telemetry ingestion API",
        "- `web/app/api/login/route.ts` — Web dashboard password / credential verification",
        "",
        "## Native Tauri desktop shell (desktop/)",
        "- `desktop/src-tauri/src/main.rs` — Windows native overlay window, tray icon, Alt+F5 pin, Win32 Orb drag, work-area overlay & click-through",
        "- `desktop/src-tauri/tauri.conf.json` — Tauri v2 window configuration",
        "- `desktop/src-tauri/capabilities/default.json` — Remote URL ACL capabilities for the hosted web UI",
    ]


def _section_other() -> list[str]:
    return [
        "",
        "## Other top-level directories",
        "- `services/whatsapp/` — Node bridge that connects WhatsApp Web to Zoro",
        "- `data/` — runtime state (logs, notes, config, reminders) — gitignored",
        "- `models/` — local model files (wake word, etc.) — gitignored",
    ]


def build() -> str:
    header = [
        "# Zoro self-map",
        "",
        "This is your own source code. You are an autonomous AI assistant and voice agent",
        "whose repository lives at `C:/Users/senze/zoro`. Both the Python backend (`src/`)",
        "and the Next.js web frontend (`web/`) belong entirely to your system. When the user",
        "asks you to change, improve, or extend yourself or your web HUD, use the module",
        "list below to know which file to point your Azure Autonomous Engineer at via the",
        "`delegate_task` / `ask_claude_code` tool.",
        "",
    ]
    parts = (
        header
        + _section_src()
        + _section_web()
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
