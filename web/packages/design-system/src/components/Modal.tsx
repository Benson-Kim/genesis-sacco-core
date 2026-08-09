"use client";

import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import styles from "./Modal.module.css";

export interface ModalProps {
    title: string;
    children: ReactNode;
    onClose: () => void;
    /** Block overlay/✕/Escape dismissal while a mutation is in flight. */
    closeDisabled?: boolean;
    /**
     * Opt-out of click-on-overlay dismissal (W56-3): form-bearing
     * drawers set this false so a stray click outside a half-completed
     * form never silently discards operator input — closing then
     * requires the explicit ✕ button or Escape. Read-only surfaces
     * keep the default overlay dismissal.
     */
    dismissOnOverlay?: boolean;
    /** drawer = right-hand panel (prototype drawer); dialog = centered box. */
    variant?: "drawer" | "dialog";
}

/** Elements a modal focus trap may move focus to. */
const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Overlay container for the prototype drawer and confirm dialogs. Content
 * is React children only (no raw markup). Dismissal is click-on-overlay,
 * the ✕ button or Escape; all are disabled while `closeDisabled` (a destructive action must never lose its in-flight state — concurrency safety UX).
 *
 * Accessibility (review finding):
 * - Focus moves INTO the dialog on open and is TRAPPED there (Tab and
 *   Shift+Tab cycle within) until the modal closes.
 * - On close, focus RETURNS to the element that was focused when the
 *   modal opened (the triggering control).
 * - Escape closes, honouring `closeDisabled`.
 */
export function Modal({
    title,
    children,
    onClose,
    closeDisabled = false,
    dismissOnOverlay = true,
    variant = "drawer",
}: Readonly<ModalProps>) {
    const dialogRef = useRef<HTMLDivElement>(null);

    // Initial focus + return-focus. Runs once per modal instance.
    useEffect(() => {
        const opener =
            document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const dialog = dialogRef.current;
        if (dialog !== null) {
            const focusables = dialog.querySelectorAll<HTMLElement>(FOCUSABLE);
            // Prefer the first focusable AFTER the ✕ button (the content),
            // falling back to the ✕, then the dialog itself.
            (focusables[1] ?? focusables[0] ?? dialog).focus();
        }
        return () => {
            // Return focus only if it is still inside the (now unmounting)
            // modal or on body — never steal focus the operator moved elsewhere.
            const active = document.activeElement;
            const stillInside =
                active === null || active === document.body || (dialog?.contains(active) ?? false);
            if (stillInside && opener !== null && opener.isConnected) opener.focus();
        };
    }, []);

    function trapKeys(event: KeyboardEvent<HTMLDivElement>) {
        // Nested modals (confirm dialog inside a drawer): the innermost
        // dialog handles the key; ancestors must not double-handle it.
        if (event.defaultPrevented) return;
        if (event.key === "Escape") {
            event.stopPropagation();
            if (!closeDisabled) onClose();
            return;
        }
        if (event.key !== "Tab") return;
        event.stopPropagation();
        const dialog = dialogRef.current;
        if (dialog === null) return;
        const focusables = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE));
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (first === undefined || last === undefined) return;
        const active = document.activeElement;
        if (event.shiftKey) {
            if (active === first || !dialog.contains(active)) {
                event.preventDefault();
                last.focus();
            }
        } else if (active === last || !dialog.contains(active)) {
            event.preventDefault();
            first.focus();
        }
    }

    return (
        <div
            className={variant === "dialog" ? styles.overlayCenter : styles.overlay}
            onClick={(event) => {
                if (event.target === event.currentTarget && !closeDisabled && dismissOnOverlay) {
                    onClose();
                }
            }}
            role="presentation"
        >
            <div
                ref={dialogRef}
                className={variant === "dialog" ? styles.dialog : styles.drawer}
                role="dialog"
                aria-modal="true"
                aria-label={title}
                tabIndex={-1}
                onKeyDown={trapKeys}
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

