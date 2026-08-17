"use client";

/**
 * IdleLogoutGuard — signs this tab out after a code-owned inactivity
 * window (#35). Mounted once inside RequireAuth, so it only runs on
 * authenticated surfaces.
 *
 * Behaviour (falsifiable legs, asserted in __tests__/idle-logout.test.tsx):
 * - Idle expiry performs EXACTLY ONE revocation through the EXISTING
 *   `logout()` path (single-flight guard) and lands on the login gate
 *   via the session store flip RequireAuth already reacts to.
 * - Tracked activity before expiry resets the window — ZERO logout
 *   calls happen.
 * - The final-minute warning is an accessible alertdialog: the shared
 *   Modal traps focus; every explicit interaction with it (button, ✕,
 *   Escape) counts as "stay signed in".
 * - Fail-closed: an evaluation error or a wire-dead revocation still
 *   ends in a cleared session (logout()'s `finally` custody).
 *
 * Ambient pointer/key activity is ignored WHILE the warning shows —
 * staying signed in from the warning is an explicit decision, never a
 * stray mouse move (the focus trap points the keyboard at exactly that
 * decision).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Modal } from "@genesis/design-system";
import { logout } from "../api";
import { useSession } from "../useSession";
import {
  ACTIVITY_EVENTS,
  ACTIVITY_THROTTLE_MS,
  IDLE_TICK_MS,
  IDLE_WARNING_SECONDS,
  IDLE_WINDOW_SECONDS,
} from "../idleLogout";
import styles from "./IdleLogoutGuard.module.css";

export function IdleLogoutGuard() {
  const authed = useSession();
  /** null = no warning; a number = seconds shown in the countdown. */
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null);
  const lastActivityRef = useRef<number>(Date.now());
  const warningRef = useRef(false);
  const expiredRef = useRef(false);

  /** Exactly-one revocation on idle expiry (single-flight guard). */
  const expire = useCallback(async () => {
    if (expiredRef.current) return;
    expiredRef.current = true;
    try {
      // The EXISTING logout path: revokes the refresh-token family and
      // clears the session + session-scoped stores in its `finally`,
      // so credentials die even when the wire call does.
      await logout();
    } catch {
      // Fail-closed: revocation failed at the wire — logout() already
      // cleared client custody; RequireAuth lands on the login gate.
    }
  }, []);

  useEffect(() => {
    if (!authed) return undefined;
    lastActivityRef.current = Date.now();
    warningRef.current = false;
    expiredRef.current = false;
    let lastRecorded = 0;

    function recordActivity() {
      // Explicit-decision rule: ambient activity never dismisses the
      // warning and a signed-out tab never resurrects its window.
      if (warningRef.current || expiredRef.current) return;
      const now = Date.now();
      if (now - lastRecorded < ACTIVITY_THROTTLE_MS) return;
      lastRecorded = now;
      lastActivityRef.current = now;
    }

    function evaluate() {
      try {
        if (expiredRef.current) return;
        const remainingMs = IDLE_WINDOW_SECONDS * 1000 - (Date.now() - lastActivityRef.current);
        if (remainingMs <= 0) {
          warningRef.current = false;
          setSecondsLeft(null);
          void expire();
          return;
        }
        if (remainingMs <= IDLE_WARNING_SECONDS * 1000) {
          warningRef.current = true;
          setSecondsLeft(Math.ceil(remainingMs / 1000));
        } else {
          warningRef.current = false;
          setSecondsLeft(null);
        }
      } catch {
        // Fail-closed: a broken evaluation must never leave the
        // session open-ended.
        void expire();
      }
    }

    function onVisibility() {
      // Returning to a tab is NOT activity: re-evaluate immediately so
      // a tab hidden past its window (background timers are throttled)
      // lands on the login gate now, not one tick later.
      if (document.visibilityState === "visible") evaluate();
    }

    for (const eventName of ACTIVITY_EVENTS) {
      window.addEventListener(eventName, recordActivity, { passive: true });
    }
    document.addEventListener("visibilitychange", onVisibility);
    const timer = window.setInterval(evaluate, IDLE_TICK_MS);
    return () => {
      for (const eventName of ACTIVITY_EVENTS) {
        window.removeEventListener(eventName, recordActivity);
      }
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearInterval(timer);
    };
  }, [authed, expire]);

  if (!authed || secondsLeft === null) return null;

  function stay() {
    lastActivityRef.current = Date.now();
    warningRef.current = false;
    setSecondsLeft(null);
  }

  return (
    <Modal
      title="Still there?"
      role="alertdialog"
      variant="dialog"
      onClose={stay}
      dismissOnOverlay={false}
    >
      <div className={styles.body}>
        {/* The countdown digits are aria-hidden so the alertdialog
            announces one stable sentence instead of re-announcing every
            second; the visible digits stay for sighted users. */}
        <p className={styles.text}>
          You have been inactive. For security this session signs out in
          under a minute
          <span className={styles.countdown} aria-hidden="true">
            {" "}
            ({secondsLeft}s)
          </span>
          . Any unsaved form input in open drawers will be discarded.
        </p>
        <div className={styles.actions}>
          <Button type="button" onClick={() => void expire()}>
            Sign out now
          </Button>
          <Button type="button" variant="primary" onClick={stay}>
            Stay signed in
          </Button>
        </div>
      </div>
    </Modal>
  );
}
