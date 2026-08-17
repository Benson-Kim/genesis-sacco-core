"use client";

import type { ReactNode } from "react";
import styles from "./Modal.module.css";

export interface ModalProps {
  title: string;
  children: ReactNode;
  onClose: () => void;
  /** Block overlay/✕ dismissal while a mutation is in flight. */
  closeDisabled?: boolean;
  /** drawer = right-hand panel (prototype drawer); dialog = centered box. */
  variant?: "drawer" | "dialog";
}

/**
 * Overlay container for the prototype drawer and confirm dialogs. Content
 * is React children only (no raw markup). Dismissal is click-on-overlay
 * or the ✕ button; both are disabled while `closeDisabled` (a destructive
 * action must never lose its in-flight state — gate 1.4 UX).
 */
export function Modal({
  title,
  children,
  onClose,
  closeDisabled = false,
  variant = "drawer",
}: Readonly<ModalProps>) {
  return (
    <div
      className={variant === "dialog" ? styles.overlayCenter : styles.overlay}
      onClick={(event) => {
        if (event.target === event.currentTarget && !closeDisabled) onClose();
      }}
      role="presentation"
    >
      <div
        className={variant === "dialog" ? styles.dialog : styles.drawer}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className={styles.head}>
          <div className={styles.title}>{title}</div>
          <button
            type="button"
            className={styles.close}
            onClick={onClose}
            disabled={closeDisabled}
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
