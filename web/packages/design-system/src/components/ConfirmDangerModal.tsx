"use client";

/**
 * Typed-confirmation dialog for destructive / money-adjacent actions
 * (security primitive — one copy, reuse-first).
 *
 * The confirm button stays disabled until the operator TYPES the exact
 * confirmation phrase (byte-identical, case-sensitive) — a deliberate
 * speed bump that makes reflex-clicking through a destructive action
 * structurally impossible. While the action is pending every dismissal
 * path is blocked and the confirm button short-circuits (exactly one effect per confirmation — concurrency safety UX; the Idempotency-Key on the mutation does the wire-level work).
 *
 * Everything renders as React text nodes — attacker-influenced phrases
 * (member names, account labels) stay inert (least disclosure).
 */
import { useId, useState, type ReactNode } from "react";
import { Banner } from "./Banner";
import { Button } from "./Button";
import { Field } from "./Field";
import { Modal } from "./Modal";
import styles from "./ConfirmDangerModal.module.css";

export interface ConfirmDangerModalProps {
  /** Dialog title, e.g. "Suspend user". */
  title: string;
  /** The exact phrase the operator must type, e.g. the member's number. */
  confirmPhrase: string;
  /** Danger button label, e.g. "Suspend user". */
  confirmLabel: string;
  /** What is about to happen — rendered above the typed field. */
  children?: ReactNode;
  /** True while the mutation is in flight: blocks dismissal + re-clicks. */
  pending?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmDangerModal({
  title,
  confirmPhrase,
  confirmLabel,
  children,
  pending = false,
  onConfirm,
  onClose,
}: Readonly<ConfirmDangerModalProps>) {
  const [typed, setTyped] = useState("");
  const inputId = useId();
  const armed = typed === confirmPhrase;

  return (
    <Modal title={title} onClose={onClose} closeDisabled={pending} variant="dialog">
      {children}
      <Banner variant="error">
        This action cannot be reflex-clicked. Type{" "}
        <span className={styles.phrase}>{confirmPhrase}</span> to confirm.
      </Banner>
      <Field label={`Type "${confirmPhrase}" to confirm`} htmlFor={inputId}>
        <input
          id={inputId}
          className={styles.input}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          disabled={pending}
        />
      </Field>
      <div className={styles.actions}>
        <Button type="button" onClick={onClose} disabled={pending}>
          Cancel
        </Button>
        <Button
          type="button"
          variant="danger"
          disabled={!armed || pending}
          onClick={() => {
            if (armed && !pending) onConfirm();
          }}
        >
          {pending ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}
