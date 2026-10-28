"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

/**
 * Shared widget limiter: at most one BIG widget plus two SMALL widgets may be
 * on screen at once, across every surface — the grid widgets owned by
 * <ZoroDisplay> and the chat-history panel owned by <Transcript>.
 *
 * Each surface registers a stable id when it opens (a WidgetKind for grid
 * widgets, "chat" for the transcript). Opening a second big widget replaces
 * the current big one; opening a third small widget evicts the oldest small
 * one. Owners watch {@link allowed} and close whatever has fallen out.
 *
 * {@link layout} describes what's currently on screen so the stage can size
 * and place the widgets (big centre, smalls stacked beside it).
 */
export const MAX_BIG = 1;
export const MAX_SMALL = 2;

export type WidgetSize = "big" | "small";

/** Content-heavy widgets that deserve the large centre slot. Everything
 *  else (glanceable cards and the chat log) is small. */
const BIG_IDS = new Set<string>([
  "search",
  "briefing",
  "vision",
  "claude",
  "github",
  "shell",
  "youtube",
  "browser",
]);

export function widgetSize(id: string): WidgetSize {
  return BIG_IDS.has(id) ? "big" : "small";
}

export type WidgetLayout = {
  /** The open big widget id, if any. */
  big: string | null;
  /** Open small widget ids, oldest → newest. */
  small: string[];
};

type WidgetLimit = {
  /** Ids currently permitted on screen. */
  allowed: Set<string>;
  layout: WidgetLayout;
  /** Register `id` as open. A new id past its size cap evicts the oldest of
   *  that size; re-opening an already-open id is a no-op. */
  open: (id: string) => void;
  /** Drop `id` from the registry (manual close / dismiss). */
  close: (id: string) => void;
  /** Drop every id — used by the "close all" frame. */
  clear: () => void;
};

const WidgetLimitContext = createContext<WidgetLimit | null>(null);

export function WidgetLimitProvider({ children }: { children: ReactNode }) {
  // Oldest → newest, no duplicates, already trimmed to the size caps.
  const [order, setOrder] = useState<string[]>([]);

  const open = useCallback((id: string) => {
    setOrder((prev) => {
      if (prev.includes(id)) return prev; // already open — not a new open
      const size = widgetSize(id);
      const cap = size === "big" ? MAX_BIG : MAX_SMALL;
      let next = [...prev, id];
      const sameSize = next.filter((x) => widgetSize(x) === size);
      if (sameSize.length > cap) {
        // Evict the oldest of the same size (both big → the old one closes).
        const evict = new Set(sameSize.slice(0, sameSize.length - cap));
        next = next.filter((x) => !evict.has(x));
      }
      return next;
    });
  }, []);

  const close = useCallback((id: string) => {
    setOrder((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev,
    );
  }, []);

  const clear = useCallback(() => {
    setOrder((prev) => (prev.length ? [] : prev));
  }, []);

  const allowed = useMemo(() => new Set(order), [order]);
  const layout = useMemo<WidgetLayout>(
    () => ({
      big: order.find((x) => widgetSize(x) === "big") ?? null,
      small: order.filter((x) => widgetSize(x) === "small"),
    }),
    [order],
  );

  const value = useMemo<WidgetLimit>(
    () => ({ allowed, layout, open, close, clear }),
    [allowed, layout, open, close, clear],
  );

  return (
    <WidgetLimitContext.Provider value={value}>
      {children}
    </WidgetLimitContext.Provider>
  );
}

export function useWidgetLimit(): WidgetLimit {
  const ctx = useContext(WidgetLimitContext);
  if (!ctx) {
    throw new Error("useWidgetLimit must be used within a WidgetLimitProvider");
  }
  return ctx;
}

/**
 * Positions one widget inside the <WidgetStage> grid according to the
 * current layout. Marked as a hit region so the desktop overlay stays
 * clickable over it.
 */
export function WidgetSlot({ id, children }: { id: string; children: ReactNode }) {
  const { layout } = useWidgetLimit();
  const size = widgetSize(id);
  const area = size === "big" ? "b" : `s${layout.small.indexOf(id) + 1}`;
  return (
    <div
      className="zoro-widget-slot"
      data-size={size}
      data-zoro-hit=""
      style={{ gridArea: area }}
    >
      {children}
    </div>
  );
}

/** Grid container that lays out the open widgets (see globals.css). */
export function WidgetStage({
  expanded,
  children,
}: {
  /** True when the desktop overlay covers the screen: stage is fixed centre. */
  expanded?: boolean;
  children: ReactNode;
}) {
  const { layout } = useWidgetLimit();
  const key = `b${layout.big ? 1 : 0}s${Math.min(layout.small.length, MAX_SMALL)}`;
  return (
    <div
      className="zoro-widget-stage"
      data-layout={key}
      data-expanded={expanded ? "true" : undefined}
    >
      {children}
    </div>
  );
}
