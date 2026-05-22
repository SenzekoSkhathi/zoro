import { NextResponse } from "next/server";

// Reads Zoro's own telemetry (Supabase) server-side and hands the web HUD
// a compact snapshot of "how she's doing": today's success rate, recent
// activity, open lessons, and last night's self-improvement reflection.
// The service key stays server-side — the browser only sees the summary.
//
// Not cached: this is live instrument data, must reflect the latest rows.
export const dynamic = "force-dynamic";

const TIMEOUT_MS = 6000;

function supabase(): { url: string; headers: Record<string, string> } | null {
  const url = process.env.SUPABASE_URL?.replace(/\/$/, "");
  const key = process.env.SUPABASE_SERVICE_KEY;
  if (!url || !key) return null;
  return {
    url,
    headers: { apikey: key, Authorization: `Bearer ${key}` },
  };
}

async function sb<T>(
  base: { url: string; headers: Record<string, string> },
  path: string,
  extraHeaders?: Record<string, string>,
): Promise<{ rows: T[]; contentRange: string | null }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${base.url}/rest/v1/${path}`, {
      headers: { ...base.headers, ...extraHeaders },
      signal: ctrl.signal,
      cache: "no-store",
    });
    const rows = (await res.json()) as T[];
    return { rows, contentRange: res.headers.get("content-range") };
  } finally {
    clearTimeout(timer);
  }
}

type EpisodeRow = { tool: string; outcome: string; latency_ms: number | null };
type LessonRow = { lesson: string; tool: string };
type ReportRow = { summary: string; report_date: string; episode_stats: unknown };

export async function GET() {
  const base = supabase();
  if (!base) {
    return NextResponse.json(
      { error: "telemetry backend not configured" },
      { status: 503 },
    );
  }

  const sinceISO = new Date(Date.now() - 7 * 86400_000).toISOString();
  const since = encodeURIComponent(sinceISO);

  try {
    const [eps, lessons, reports] = await Promise.all([
      sb<EpisodeRow>(
        base,
        `zoro_episodes?select=tool,outcome,latency_ms&created_at=gte.${since}&order=created_at.desc&limit=2000`,
      ),
      sb<LessonRow>(
        base,
        `zoro_lessons?select=lesson,tool&status=eq.open&order=created_at.desc&limit=5`,
      ),
      sb<ReportRow>(
        base,
        `zoro_daily_reports?select=summary,report_date,episode_stats&order=report_date.desc&limit=1`,
      ),
    ]);

    const rows = eps.rows ?? [];
    // Don't let voice-plumbing events skew the success rate the user sees.
    const real = rows.filter((r) => r.tool !== "false_interruption");
    const success = real.filter((r) => r.outcome === "success").length;
    const error = real.filter((r) => r.outcome === "error").length;
    const aborted = real.filter((r) => r.outcome === "aborted").length;
    const corrected = real.filter((r) => r.outcome === "corrected").length;
    const total = real.length;

    // Per-tool counts → the few most-used tools, for a live "top systems"
    // readout.
    const byTool = new Map<string, number>();
    for (const r of real) byTool.set(r.tool, (byTool.get(r.tool) ?? 0) + 1);
    const topTools = [...byTool.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([tool, count]) => ({ tool, count }));

    const latencies = real
      .map((r) => r.latency_ms)
      .filter((n): n is number => typeof n === "number" && n > 0)
      .sort((a, b) => a - b);
    const medianLatency = latencies.length
      ? latencies[Math.floor(latencies.length / 2)]
      : null;

    const report = reports.rows?.[0] ?? null;

    return NextResponse.json({
      window_days: 7,
      counts: { total, success, error, aborted, corrected },
      success_rate: total ? success / total : null,
      median_latency_ms: medianLatency,
      top_tools: topTools,
      open_lessons: (lessons.rows ?? []).map((l) => ({
        lesson: l.lesson,
        tool: l.tool,
      })),
      last_report: report
        ? { summary: report.summary, date: report.report_date }
        : null,
    });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "telemetry fetch failed" },
      { status: 502 },
    );
  }
}
