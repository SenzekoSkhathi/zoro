"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const WAKE_WS_URL = "ws://127.0.0.1:8765";

type WakeStatus = "offline" | "connected";

/**
 * Connects to the local on-device wake-word service (wake_service.py) and
 * fires `onWake` instantly when the wake word is detected. Auto-reconnects
 * so the order of starting the service vs. the page doesn't matter.
 *
 * pause()/resume() tell the service to stop listening while Zoro is in a
 * call, so her own voice can't re-trigger the wake word.
 */
export function useWakeService(onWake: () => void) {
  const [status, setStatus] = useState<WakeStatus>("offline");
  const wsRef = useRef<WebSocket | null>(null);
  const onWakeRef = useRef(onWake);
  onWakeRef.current = onWake;

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(WAKE_WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setStatus("connected");
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
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback((cmd: "pause" | "resume") => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ cmd }));
    }
  }, []);

  const pause = useCallback(() => send("pause"), [send]);
  const resume = useCallback(() => send("resume"), [send]);

  return { status, pause, resume };
}
