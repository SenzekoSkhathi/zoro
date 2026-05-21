"use client";

import type { CSSProperties } from "react";
import styles from "./Orb.module.css";

export type OrbMode =
  | "idle"
  | "listening"
  | "connecting"
  | "thinking"
  | "speaking";

/**
 * Arc-reactor orb. Counter-rotating HUD rings around a glowing core.
 *
 * Decorative mode: `mode` drives CSS-keyframe intensity.
 * Reactive mode: pass live `energy` (0..1) and an `accent` color and the
 * core scales/glows in real time with audio — cyan for the user, a warm
 * tone for Zoro.
 */
export function Orb({
  mode = "idle",
  energy,
  accent,
  docked = false,
}: {
  mode?: OrbMode;
  energy?: number;
  accent?: string;
  /** When content is on screen the orb shrinks into the bottom-right
   * corner; clears back to its natural centred state when false. */
  docked?: boolean;
}) {
  const reactive = energy !== undefined;
  const style = {
    ...(accent ? { "--accent": accent } : {}),
    ...(reactive
      ? { "--energy": String(Math.min(1, Math.max(0, energy))) }
      : {}),
  } as CSSProperties;

  return (
    <div
      className={`${styles.orb} ${docked ? styles.docked : ""}`}
      data-mode={mode}
      data-reactive={reactive ? "true" : undefined}
      style={style}
    >
      {/* Targeting reticle layer — compass labels, corner brackets, and
          crosshairs. Sits OUTSIDE the orb's 220px box and fades out when
          the orb docks so it doesn't crowd the corner. */}
      <div className={styles.reticle} aria-hidden>
        <span className={`${styles.bracket} ${styles.bracketTL}`} />
        <span className={`${styles.bracket} ${styles.bracketTR}`} />
        <span className={`${styles.bracket} ${styles.bracketBL}`} />
        <span className={`${styles.bracket} ${styles.bracketBR}`} />
        <span className={`${styles.cross} ${styles.crossH}`} />
        <span className={`${styles.cross} ${styles.crossV}`} />
        {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => (
          <span
            key={deg}
            className={styles.tickLabel}
            style={{
              transform: `translate(-50%, -50%) rotate(${deg}deg) translate(0, -140px) rotate(${-deg}deg)`,
            }}
          >
            {String(deg).padStart(3, "0")}
          </span>
        ))}
      </div>

      <div className={`${styles.ring} ${styles.ringOuter}`} />
      <div className={`${styles.ring} ${styles.ringMid}`} />
      <div className={`${styles.ring} ${styles.ringInner}`} />
      <div className={styles.ticks} />
      <div className={styles.core}>
        <div className={styles.coreGlow} />
      </div>
    </div>
  );
}
