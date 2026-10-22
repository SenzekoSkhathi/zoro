"use client";

import { useEffect, useRef, useState } from "react";
import { useDataChannel } from "@livekit/components-react";
import styles from "./ToolTicker.module.css";

const DISPLAY_TOPIC = "zoro.display";

type ToolFrame = {
  kind: "tool";
  data: { name: string; phase: "active" | "ok" | "err"; ms?: number };
};

type Entry = {
  name: string;
  phase: "active" | "ok" | "err";
  ms?: number;
  ts: number;
};

function playThinkingChime() {
  try {
    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = "sine";
    // Soft, pleasant double-harmonic frequency (520Hz -> 780Hz)
    osc.frequency.setValueAtTime(520, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(780, ctx.currentTime + 0.06);

    // Subtle gentle volume (0.025)
    gain.gain.setValueAtTime(0.001, ctx.currentTime);
    gain.gain.linearRampToValueAtTime(0.025, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.14);

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.start();
    osc.stop(ctx.currentTime + 0.15);
  } catch {
    /* best-effort audio earcon */
  }
}

/**
 * Live activity ticker — shows the tool Zoro is invoking right now and its
 * outcome, so the HUD reflects real backend work in real time. Driven by
 * the `kind:"tool"` frames the agent's @_logged wrapper emits at the start
 * and end of every tool call. The finished line lingers briefly, then fades.
 */
export function ToolTicker() {
  const { message } = useDataChannel(DISPLAY_TOPIC);
  const [entry, setEntry] = useState<Entry | null>(null);
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!message) return;
    try {
      const parsed = JSON.parse(
        new TextDecoder().decode(message.payload),
      ) as Partial<ToolFrame>;
      if (parsed?.kind !== "tool" || !parsed.data?.name) return;
      const { name, phase, ms } = parsed.data;
      if (phase === "active") playThinkingChime();
      setEntry({ name, phase, ms, ts: Date.now() });

      if (clearTimer.current) clearTimeout(clearTimer.current);
      // Keep "active" pinned; a finished line auto-clears after a beat.
      if (phase !== "active") {
        clearTimer.current = setTimeout(() => setEntry(null), 2600);
      }
    } catch {
      /* not our frame */
    }
  }, [message]);

  useEffect(
    () => () => {
      if (clearTimer.current) clearTimeout(clearTimer.current);
    },
    [],
  );

  if (!entry) return null;

  const glyph =
    entry.phase === "active" ? "▸" : entry.phase === "ok" ? "✓" : "✕";
  const label = entry.name.toUpperCase();
  const tail =
    entry.phase === "active"
      ? "running"
      : entry.ms != null
        ? entry.ms < 1000
          ? `${entry.ms}ms`
          : `${(entry.ms / 1000).toFixed(1)}s`
        : entry.phase === "ok"
          ? "done"
          : "failed";

  return (
    <div
      className={styles.ticker}
      data-phase={entry.phase}
      // Re-key so the decode-in animation replays on each new tool.
      key={`${entry.name}-${entry.ts}`}
      aria-live="polite"
    >
      <span className={styles.glyph}>{glyph}</span>
      <span className={styles.name}>{label}</span>
      <span className={styles.dotline} aria-hidden />
      <span className={styles.tail}>{tail}</span>
    </div>
  );
}
