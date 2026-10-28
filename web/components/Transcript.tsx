"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  useTranscriptions,
  useLocalParticipant,
  useDataChannel,
} from "@livekit/components-react";
import { HudPanel } from "./HudPanel";
import { useWidgetLimit, WidgetSlot } from "./WidgetLimit";
import styles from "./Transcript.module.css";

// Same channel ZoroDisplay listens on. The agent's show_chat_history tool
// publishes {kind:"chat", data:{show}} here to toggle this panel.
const DISPLAY_TOPIC = "zoro.display";

/**
 * The conversation is captured continuously but stays OFF screen while you
 * talk. It only appears when you ask Zoro to "show me our chat history" /
 * "show me our chat interface" — the agent then pushes a {kind:"chat"}
 * toggle over the data channel and we render the accumulated exchange as a
 * scrollable chat-history panel. Must live inside <LiveKitRoom>.
 */
export function Transcript({
  onActiveChange,
}: {
  /** Fires when the chat-history panel opens (true) / closes (false). */
  onActiveChange?: (active: boolean) => void;
}) {
  const transcriptions = useTranscriptions();
  const { localParticipant } = useLocalParticipant();
  const endRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  // Chat counts as a small widget toward the shared 1 big + 2 small max.
  const { allowed, open, close } = useWidgetLimit();

  const { message } = useDataChannel(DISPLAY_TOPIC);

  useEffect(() => {
    if (!message) return;
    try {
      const text = new TextDecoder().decode(message.payload);
      const parsed = JSON.parse(text) as {
        kind?: string;
        data?: { show?: boolean; target?: string };
      };
      if (parsed?.kind === "chat") {
        const show = parsed.data?.show !== false;
        setVisible(show);
        if (show) open("chat");
        else close("chat");
      } else if (parsed?.kind === "close") {
        const t = parsed.data?.target;
        if (t === "chat" || t === "all") {
          setVisible(false);
          close("chat");
        }
      }
    } catch {
      /* not our frame */
    }
  }, [message, open, close]);

  // The panel is on screen only while it's both asked-for and still permitted
  // by the shared limiter — opening a third small surface evicts "chat" from
  // `allowed`, which hides this without touching `visible`.
  const shown = visible && allowed.has("chat");

  // History accumulates regardless of visibility, so it's complete the
  // moment Zoro is asked to show it.
  const lines = useMemo(
    () =>
      transcriptions
        .filter((t) => t.text.trim().length > 0)
        .map((t) => ({
          id: t.streamInfo.id,
          mine: t.participantInfo.identity === localParticipant.identity,
          text: t.text,
        })),
    [transcriptions, localParticipant.identity],
  );

  useEffect(() => {
    if (shown) {
      endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [lines.length, shown]);

  useEffect(() => {
    onActiveChange?.(shown);
  }, [shown, onActiveChange]);

  if (!shown) return null;

  return (
    <WidgetSlot id="chat">
      <HudPanel
        kind="CHAT LOG"
        meta={`${lines.length} LINE${lines.length === 1 ? "" : "S"}`}
        ariaLabel="Chat history"
        role="log"
        headerExtra={
          <button
            className={styles.close}
            onClick={() => {
              setVisible(false);
              close("chat");
            }}
            aria-label="Close chat history"
          >
            ✕
          </button>
        }
      >
        {lines.length === 0 ? (
          <p className={styles.empty}>No conversation yet.</p>
        ) : (
          lines.map((l) => (
            <p
              key={l.id}
              className={`${styles.line} ${l.mine ? styles.you : styles.zoro}`}
            >
              <span className={styles.who}>{l.mine ? "You" : "Zoro"}</span>
              <span className={styles.text}>{l.text}</span>
            </p>
          ))
        )}
        <div ref={endRef} />
      </HudPanel>
    </WidgetSlot>
  );
}
