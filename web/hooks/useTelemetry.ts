"use client";

import { useEffect, useState } from "react";

export type Telemetry = {
  window_days: number;
  counts: {
    total: number;
    success: number;
    error: number;
    aborted: number;
    corrected: number;
  };
  success_rate: number | null;
  median_latency_ms: number | null;
  top_tools: { tool: string; count: number }[];
  open_lessons: { lesson: string; tool: string }[];
  last_report: { summary: string; date: string } | null;
};

/**
 * Polls /api/telemetry so the HUD reflects Zoro's live self-state (success
 * rate, activity, learning). Slow cadence — this is ambient instrumentation,
 * not a hot path, and the laptop is already busy running the agent.
 */
export function useTelemetry(intervalMs = 15000): Telemetry | null {
  const [data, setData] = useState<Telemetry | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/api/telemetry", { cache: "no-store" });
        if (!res.ok) return;
        const json = (await res.json()) as Telemetry;
        if (alive) setData(json);
      } catch {
        /* offline / backend down — keep last good reading */
      }
    };
    void load();
    const id = setInterval(load, intervalMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [intervalMs]);

  return data;
}
