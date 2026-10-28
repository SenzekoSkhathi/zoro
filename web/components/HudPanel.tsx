"use client";

import type { ReactNode } from "react";
import styles from "./HudPanel.module.css";

/**
 * Shared ARC-HUD shell. Every card Zoro projects (and the chat-history
 * panel) wraps its content in this so the chrome — chamfered frame,
 * hairline border, corner reticles, instrument header, scan texture and
 * decode-in motion — is identical everywhere.
 *
 * `kind` is the instrument tag (SEARCH, WEATHER, …); `meta` is the small
 * monospace readout on the right (query, place, count); `accent` flips the
 * palette to gold for Zoro's own proactive voice.
 */
export function HudPanel({
  kind,
  meta,
  accent = "cyan",
  ariaLabel,
  role,
  onClose,
  headerExtra,
  children,
}: {
  kind: string;
  meta?: ReactNode;
  accent?: "cyan" | "gold";
  ariaLabel: string;
  role?: string;
  /** When provided, a small ✕ button is rendered in the header so the
   * widget can be dismissed by click as well as by voice. */
  onClose?: () => void;
  headerExtra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      className={styles.panel}
      data-accent={accent}
      aria-label={ariaLabel}
      role={role}
    >
      <span className={styles.fill} aria-hidden />
      <span className={styles.texture} aria-hidden />

      <header className={styles.head}>
        <span className={styles.kicker}>{kind}</span>
        <span className={styles.rule} aria-hidden />
        {meta != null && <span className={styles.meta}>{meta}</span>}
        {headerExtra}
        {onClose && (
          <button
            type="button"
            className={styles.close}
            onClick={onClose}
            aria-label={`Close ${kind} widget`}
          >
            ✕
          </button>
        )}
      </header>

      <div className={styles.body}>{children}</div>

      <span className={`${styles.reticle} ${styles.tl}`} aria-hidden />
      <span className={`${styles.reticle} ${styles.tr}`} aria-hidden />
      <span className={`${styles.reticle} ${styles.bl}`} aria-hidden />
      <span className={`${styles.reticle} ${styles.br}`} aria-hidden />
    </section>
  );
}
