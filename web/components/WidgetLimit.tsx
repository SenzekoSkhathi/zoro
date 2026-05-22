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
 * Shared two-widget limiter.
 *
 * At most {@link MAX_WIDGETS} widgets may be on screen at once across every
 * surface — the grid widgets owned by <ZoroDisplay> and the chat-history
 * panel owned by <Transcript>. Each surface registers a stable id when it
 * opens (a WidgetKind for grid widgets, "chat" for the transcript) and the
 * limiter keeps only the most-recent {@link MAX_WIDGETS} ids. Opening a third
 * surface drops the oldest (first-opened) id; owners watch {@link allowed}
 * and close whatever has fallen out.
 */
export const MAX_WIDGETS = 2;

type WidgetLimit = {
  /** Ids currently permitted on screen (at most {@link MAX_WIDGETS}). */
  allowed: Set<string>;
  /** Register/refresh `id` as open. A genuinely new id past the cap evicts
   *  the oldest; re-opening an already-open id is a no-op. */
  open: (id: string) => void;
  /** Drop `id` from the registry (manual close / dismiss). */
  close: (id: string) => void;
  /** Drop every id — used by the "close all" frame. */
  clear: () => void;
};

const WidgetLimitContext = createContext<WidgetLimit | null>(null);

export function WidgetLimitProvider({ children }: { children: ReactNode }) {
  // Oldest → newest, no duplicates. Trimmed to the most-recent MAX_WIDGETS.
  const [order, setOrder] = useState<string[]>([]);

  const open = useCallback((id: string) => {
    setOrder((prev) => {
      if (prev.includes(id)) return prev; // already open — not a new open
      const next = [...prev, id];
      return next.length > MAX_WIDGETS
        ? next.slice(next.length - MAX_WIDGETS) // evict oldest (first-opened)
        : next;
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

  const value = useMemo<WidgetLimit>(
    () => ({ allowed, open, close, clear }),
    [allowed, open, close, clear],
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
