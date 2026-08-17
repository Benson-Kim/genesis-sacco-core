"use client";

import { useEffect, useState, type ReactNode } from "react";
import styles from "./FrameGuard.module.css";

/**
 * Refuses to run the console inside a frame (clickjacking on destructive
 * admin actions — carried from the !25 threat model). Defence in depth:
 * the primary control is `frame-ancestors 'none'` + `X-Frame-Options:
 * DENY` from next.config.ts; this guard covers header-stripping proxies.
 *
 * `isFramed` is injectable so the guard has a falsifiable test.
 */
export function defaultIsFramed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.top !== window.self;
  } catch {
    // Cross-origin access to window.top throws => we ARE framed.
    return true;
  }
}

export function FrameGuard({
  children,
  isFramed = defaultIsFramed,
}: {
  children: ReactNode;
  isFramed?: () => boolean;
}) {
  // Effect-based so SSR/hydration stay deterministic; the decision runs
  // before any privileged data is fetched into the frame.
  const [framed, setFramed] = useState(false);
  useEffect(() => {
    if (isFramed()) setFramed(true);
  }, [isFramed]);

  if (framed) {
    return (
      <div className={styles.blocked} role="alert">
        This console cannot run inside a frame. Open it directly.
      </div>
    );
  }
  return <>{children}</>;
}
