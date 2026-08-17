"use client";

/**
 * Skip-to-content link (accessibility primitive):
 * the first focusable element in the shell; visually hidden until
 * focused, then a visible 44px+ target. Jumps keyboard/AT users past the
 * sidebar navigation straight to the main content region.
 */
import styles from "./SkipToContent.module.css";

export const MAIN_CONTENT_ID = "main-content";

export function SkipToContent() {
  return (
    <a className={styles.skip} href={`#${MAIN_CONTENT_ID}`}>
      Skip to content
    </a>
  );
}
