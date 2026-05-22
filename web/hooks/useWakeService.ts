"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const WAKE_WS_URL = "ws://127.0.0.1:8765";

// While a call is live the browser holds the wake service's mic-pause open by
// re-sending "pause" on this interval. The service treats a pause as a lease
// that expires shortly after the last refresh (PAUSE_TTL_S, server-side), so
// this must be comfortably shorter than that TTL. The point: if this page
// crashes, navigates, or its ws churns mid-call, the lease lapses on its own
// and the mic starts listening again instead of going permanently deaf.
const PAUSE_REFRESH_MS = 4000;

type WakeStatus = "offline" | "connected";

/**
 * Connects to the local on-device wake-word service (wake_service.py) and
 * fires `onWake` instantly when the wake word is detected. Auto-reconnects
 * so the order of starting the service vs. the page doesn't matter.
 *
 * pause()/resume() tell the service to stop listening while Zoro is in a
 * call, so her own voice can't re-trigger the wake word. pause() is a *lease*
 * we keep refreshing for as long as the call is live, and it re-asserts itself
 * over a freshly reconnected socket — so the pause can never get stranded.
 */
export function useWakeService(onWake: () => void) {
  const [status, setStatus] = useState<WakeStatus>("offline");
  const wsRef = useRef<WebSocket | null>(null);
  // Whether a call-pause is currently meant to be in effect. Held in a ref so
  // the reconnect handler can re-pause a new socket without re-running effects.
  const pausedRef = useRef(false);
  const refreshRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const onWakeRef = useRef(onWake);
  onWakeRef.current = onWake;

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(WAKE_WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setStatus("connected");
        // A call may already be live (this is a reconnect mid-call). Re-assert
        // the pause lease on the new socket so the mic doesn't pop back to life.
        if (pausedRef.current) {
          ws.send(JSON.stringify({ cmd: "pause" }));
        }
      };
      ws.onmessage = (ev) => {
        try {
          const evt = JSON.parse(ev.data).event;
          // "proactive" is Zoro reaching out first; it connects exactly
          // like a wake word so the agent can deliver a queued directive.
          if (evt === "wake" || evt === "proactive") onWakeRef.current();
        } catch {
          /* ignore malformed frames */
        }
      };
      const reconnect = () => {
        setStatus("offline");
        if (!closed) retry = setTimeout(connect, 1500);
      };
      ws.onclose = reconnect;
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (refreshRef.current) clearInterval(refreshRef.current);
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback((cmd: "pause" | "resume") => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ cmd }));
    }
  }, []);

  // Open the pause lease and keep refreshing it for the life of the call.
  // Idempotent: calling pause() again while already paused just keeps the
  // existing keepalive running.
  const pause = useCallback(() => {
    pausedRef.current = true;
    send("pause");
    if (!refreshRef.current) {
      refreshRef.current = setInterval(() => send("pause"), PAUSE_REFRESH_MS);
    }
  }, [send]);

  // Drop the lease: stop refreshing and tell the service to listen again. (Even
  // if this resume frame is lost, the lease lapses on its own server-side.)
  const resume = useCallback(() => {
    pausedRef.current = false;
    if (refreshRef.current) {
      clearInterval(refreshRef.current);
      refreshRef.current = undefined;
    }
    send("resume");
  }, [send]);

  return { status, pause, resume };
}
