# Zoro self-map

This is your own source code. You are an autonomous AI assistant and voice agent
whose repository lives at `C:/Users/senze/zoro`. Both the Python backend (`src/`)
and the Next.js web frontend (`web/`) belong entirely to your system. When the user
asks you to change, improve, or extend yourself or your web HUD, use the module
list below to know which file to point your Azure Autonomous Engineer at via the
`delegate_task` / `ask_claude_code` tool.

## Source modules (src/)
- `src/zoro_agent.py` — Zoro — voice-first assistant agent.
- `src/zoro_browser.py` — Zoro's isolated virtual browser engine — powered by Playwright Chromium.
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
- `src/zoro_prompt.py` — Zoro's identity / operating spec — the shared system prompt.
- `src/zoro_reflector.py` — Nightly self-improvement loop.
- `src/zoro_retry.py` — Bounded retry for transient network failures on idempotent tool reads.
- `src/zoro_safety.py` — Shared safety / gating layer for Zoro's autonomous behaviour.
- `src/zoro_schema_cache.py` — High-Performance Schema Caching and Event Loop Optimization for Zoro.
- `src/zoro_screen_relay.py` — Zoro Secure Universal Host & Screen Relay.
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

## Frontend web dashboard & HUD (web/)
- `web/app/page.tsx` — Main HUD dashboard: LiveKit audio visualizer, Siri Orb, HUD panels, telemetry
- `web/components/Orb.tsx` — Reactive multi-band 3D/canvas Siri Orb with audio reactivity
- `web/components/InstrumentRail.tsx` — System telemetry & resource gauges (CPU, memory, latency)
- `web/components/Transcript.tsx` — Real-time speech transcript & conversation history
- `web/components/ToolTicker.tsx` — Active tool execution ticker and progress indicators
- `web/components/HudPanel.tsx` — Collapsible HUD information drawer
- `web/components/LoginGate.tsx` — Authentication & session gate for web HUD
- `web/app/api/token/route.ts` — LiveKit WebRTC access token generation API
- `web/app/api/session/route.ts` — Agent session dispatch & lifecycle control API
- `web/app/api/telemetry/route.ts` — PostgreSQL / App Insights telemetry ingestion API
- `web/app/api/login/route.ts` — Web dashboard password / credential verification

## Sub-agents (agents/)
- `agents/agent_smith.md` — Agent Smith: Sub-agent author. Writes new role files under `agents/` when Zoro decides she needs a new specialist.
- `agents/coder.md` — Coder: Full-stack code editor for the Zoro repository. Makes focused, robust changes across backend and frontend that pass the boot check.
- `agents/diagnostician.md` — Diagnostician: System troubleshooter for the Windows machine Zoro runs on. Runs PowerShell commands, inspects screen state, and iterates until the issue is resolved or clearly diagnosed.
- `agents/researcher.md` — Researcher: Read-only research assistant. Investigates topics, reads source, searches the web, and reports findings in plain prose. Never writes files.
- `agents/task_source_dispatcher.md` — Task Source Dispatcher: Reviews pending tasks, reads relevant Zoro source, and prepares one well-scoped engineering handoff.

## Scripts (scripts/)
- `scripts/build_self_map.py`
- `scripts/deploy_azure.ps1`
- `scripts/deploy_to_azure.py`
- `scripts/deploy_web.ps1`
- `scripts/engineer_logs.py`
- `scripts/google_auth.py`
- `scripts/install_autostart.ps1`
- `scripts/make_icon.py`
- `scripts/show_wa_qr.py`
- `scripts/start_screen_relay.ps1`
- `scripts/start_screen_relay.vbs`
- `scripts/uninstall_autostart.ps1`
- `scripts/validate_template.py`
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
- `data/` — runtime state (logs, notes, config, reminders) — gitignored
- `models/` — local model files (wake word, etc.) — gitignored
