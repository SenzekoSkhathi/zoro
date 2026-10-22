# Zoro self-map

This is your own source code. You are a Python voice assistant
whose code lives at `C:/Users/senze/zoro`. When the user
asks you to change, improve, or extend yourself, use the module
list below to know which file to point Claude Code at via the
`ask_claude_code` tool.

## Source modules (src/)
- `src/zoro_agent.py` — Zoro — voice-first assistant agent.
- `src/zoro_chains.py` — Sequence miner — turn recurring tool-call chains into open lessons.
- `src/zoro_claude_code.py` — Hand a task off to the Claude Code CLI running in the Zoro repo.
- `src/zoro_computer.py` — Vision-based computer-use loop — Zoro's fallback "hands".
- `src/zoro_corrections.py` — Detect when the user's next utterance is correcting Zoro's last action.
- `src/zoro_episodes.py` — Episodic outcome log — Phase 1 telemetry foundation.
- `src/zoro_github.py` — GitHub access via the gh CLI.
- `src/zoro_google.py` — Read-only Google (Calendar + Gmail) access using the token that
- `src/zoro_lessons.py` — Reflection layer — turn failed (and notable successful) episodes into lessons.
- `src/zoro_memory.py` — Zoro's long-term memory — Azure PostgreSQL (baseform-pg-za) with Supabase fallback.
- `src/zoro_proactive.py` — Zoro's proactive ambient loop — she speaks first.
- `src/zoro_prompt.py` — Zoro's identity / operating spec — the shared system prompt for Claude.
- `src/zoro_reflector.py` — Nightly self-improvement loop.
- `src/zoro_retry.py` — Bounded retry for transient network failures on idempotent tool reads.
- `src/zoro_safety.py` — Shared safety / gating layer for Zoro's autonomous behaviour.
- `src/zoro_self_mod.py` — Autonomous self-modification: caps, logging, and the picker.
- `src/zoro_shell.py` — PowerShell access + Windows app launching.
- `src/zoro_spotify.py` — Spotify Web API access using a cached OAuth token.
- `src/zoro_subagents.py` — Sub-agent loader.
- `src/zoro_supervisor.py` — Zoro always-on supervisor.
- `src/zoro_tasks.py` — Durable task queue — 'do this thing later'.
- `src/zoro_telemetry.py` — Azure Application Insights and OpenTelemetry telemetry for Zoro Assistant.
- `src/zoro_tools.py` — Pure helper logic for Zoro's tools (no LiveKit imports).
- `src/zoro_updater.py` — Safe self-update for Zoro's Python backend.
- `src/zoro_whatsapp.py` — WhatsApp access via the local Node bridge (services/whatsapp).
- `src/zoro_youtube.py` — YouTube access: search + transcript fetch + Claude-powered summary.
- `src/wake_service.py` — Zoro on-device wake-word service.

## Sub-agents (agents/)
- `agents/agent_smith.md` — Agent Smith: Sub-agent author. Writes new role files under `agents/` when Zoro decides she needs a new specialist.
- `agents/coder.md` — Coder: General-purpose code editor for the Zoro repository. Makes focused, well-scoped changes that pass the boot check.
- `agents/diagnostician.md` — Diagnostician: System troubleshooter for the Windows machine Zoro runs on. Runs PowerShell commands, inspects screen state, and iterates until the issue is resolved or clearly diagnosed.
- `agents/researcher.md` — Researcher: Read-only research assistant. Investigates topics, reads source, searches the web, and reports findings in plain prose. Never writes files.

## Scripts (scripts/)
- `scripts/build_self_map.py`
- `scripts/install_autostart.ps1`
- `scripts/make_icon.py`
- `scripts/show_wa_qr.py`
- `scripts/uninstall_autostart.ps1`
- `scripts/zoro_autostart.vbs`
- `scripts/zoro_daily_reports.sql`
- `scripts/zoro_episodes.sql`
- `scripts/zoro_flakiness.py`
- `scripts/zoro_lessons.sql`
- `scripts/zoro_self_mod_log.sql`
- `scripts/zoro_spotify_auth.py`
- `scripts/zoro_tasks.sql`

## Other top-level directories
- `services/whatsapp/` — Node bridge that connects WhatsApp Web to Zoro
- `web/` — Next.js dashboard / HUD that Zoro publishes display state to
- `data/` — runtime state (logs, notes, config, reminders) — gitignored
- `models/` — local model files (wake word, etc.) — gitignored
