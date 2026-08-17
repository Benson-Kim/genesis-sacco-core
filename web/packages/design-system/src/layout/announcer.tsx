"use client";

/**
 * Shared aria-live announcer (accessibility primitive): async success/error outcomes are announced to assistive
 * technology without moving focus. One region for the whole app (gate
 * 1.1); screens call announce() from mutation/query callbacks.
 *
 * Announcements are OPERATOR-FACING copy ONLY — never raw API data, so
 * the least-disclosure posture (least disclosure) carries into the audio layer.
 */
import { useEffect, useState } from "react";
import styles from "./announcer.module.css";

type Politeness = "polite" | "assertive";

interface Announcement {
  message: string;
  politeness: Politeness;
  /** Monotonic id so REPEATED identical messages still re-announce. */
  seq: number;
}

let seq = 0;
const listeners = new Set<(a: Announcement) => void>();

/** Announce a message to the shared live region ("polite" by default). */
export function announce(message: string, politeness: Politeness = "polite"): void {
  seq += 1;
  const payload: Announcement = { message, politeness, seq };
  listeners.forEach((listener) => listener(payload));
}

/**
 * The single live region — mounted once in the AppShell. Visually hidden,
 * never removed from the accessibility tree.
 */
export function LiveAnnouncer() {
  const [current, setCurrent] = useState<Announcement | null>(null);

  useEffect(() => {
    const listener = (a: Announcement) => setCurrent(a);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  return (
    <div
      className={styles.srOnly}
      role="status"
      aria-live={current?.politeness ?? "polite"}
      aria-atomic="true"
    >
      {/* keyed on seq so identical repeated messages re-fire */}
      {current !== null && <span key={current.seq}>{current.message}</span>}
    </div>
  );
}
