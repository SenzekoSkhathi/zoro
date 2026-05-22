"use client";

import { useEffect, useState } from "react";
import type { Telemetry } from "@/hooks/useTelemetry";
import styles from "./InstrumentRail.module.css";

/** Eased count-up so readouts spin up like an instrument instead of snapping. */
function useCountUp(target: number, ms = 700): number {
  const [v, setV] = useState(0);
  useEffect(() => {
    let raf = 0;
    const start = performance.now();
    const from = 0;
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / ms);
      setV(from + (target - from) * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return v;
}

/** Small SVG ring gauge for the success rate. Cheap to render, no loop. */
function RateGauge({ rate }: { rate: number }) {
  const pct = useCountUp(rate);
  const r = 26;
  const c = 2 * Math.PI * r;
  const dash = c * pct;
  return (
    <div className={styles.gauge}>
      <svg viewBox="0 0 64 64" className={styles.gaugeSvg} aria-hidden>
        <circle cx="32" cy="32" r={r} className={styles.gaugeTrack} />
        <circle
          cx="32"
          cy="32"
          r={r}
          className={styles.gaugeArc}
          strokeDasharray={`${dash} ${c}`}
          transform="rotate(-90 32 32)"
        />
      </svg>
      <span className={styles.gaugeLabel}>
        {Math.round(pct * 100)}
        <i>%</i>
      </span>
    </div>
  );
}

/**
 * Left-edge instrument column. Always on screen (even idle) so the app
 * reads as a live system, not a dormant page. Surfaces Zoro's real
 * telemetry: success rate, activity, latency, learning state, last
 * reflection. Polls server-side telemetry via the passed-in snapshot.
 */
export function InstrumentRail({ data }: { data: Telemetry | null }) {
  const counts = data?.counts;
  const episodes = useCountUp(counts?.total ?? 0);
  const lessons = data?.open_lessons ?? [];

  return (
    <aside className={styles.rail} aria-label="Zoro telemetry">
      <div className={styles.head}>
        <span className={styles.kicker}>CORE TELEMETRY</span>
        <span className={styles.win}>{data ? `${data.window_days}D` : "—"}</span>
      </div>

      <div className={styles.block}>
        <span className={styles.label}>SUCCESS</span>
        {data?.success_rate != null ? (
          <RateGauge rate={data.success_rate} />
        ) : (
          <span className={styles.dim}>—</span>
        )}
      </div>

      <div className={styles.metrics}>
        <div className={styles.metric}>
          <span className={styles.metricVal}>{Math.round(episodes)}</span>
          <span className={styles.metricKey}>ACTIONS</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.metricVal}>
            {data?.median_latency_ms != null
              ? data.median_latency_ms < 1000
                ? `${data.median_latency_ms}`
                : `${(data.median_latency_ms / 1000).toFixed(1)}s`
              : "—"}
            {data?.median_latency_ms != null &&
              data.median_latency_ms < 1000 && <i className={styles.unit}>ms</i>}
          </span>
          <span className={styles.metricKey}>P50 LAT</span>
        </div>
        <div className={styles.metric}>
          <span
            className={`${styles.metricVal} ${
              (counts?.error ?? 0) > 0 ? styles.warn : ""
            }`}
          >
            {counts?.error ?? 0}
          </span>
          <span className={styles.metricKey}>ERRORS</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.metricVal}>{counts?.corrected ?? 0}</span>
          <span className={styles.metricKey}>CORRECT</span>
        </div>
      </div>

      {data?.top_tools && data.top_tools.length > 0 && (
        <div className={styles.block}>
          <span className={styles.label}>TOP SYSTEMS</span>
          <ul className={styles.tools}>
            {data.top_tools.map((t) => {
              const max = data.top_tools[0].count || 1;
              return (
                <li key={t.tool} className={styles.tool}>
                  <span className={styles.toolName}>{t.tool}</span>
                  <span className={styles.toolBarTrack}>
                    <span
                      className={styles.toolBar}
                      style={{ width: `${(t.count / max) * 100}%` }}
                    />
                  </span>
                  <span className={styles.toolCount}>{t.count}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <div className={styles.block}>
        <span className={styles.label}>
          LEARNING
          <i
            className={`${styles.pulse} ${
              lessons.length > 0 ? styles.pulseOn : ""
            }`}
          />
        </span>
        <span className={styles.learnLine}>
          {lessons.length > 0
            ? `${lessons.length} open lesson${lessons.length > 1 ? "s" : ""}`
            : "no open lessons"}
        </span>
        {data?.last_report?.summary && (
          <p className={styles.reflection} title={data.last_report.summary}>
            {data.last_report.summary}
          </p>
        )}
      </div>
    </aside>
  );
}
