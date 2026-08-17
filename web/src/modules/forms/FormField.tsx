"use client";

/**
 * Shared form-field primitive (reuse-first: the remaining module screens must NOT re-implement label/error wiring).
 *
 * - PERSISTENT label (never placeholder-as-label).
 * - Inline field error (client Zod message or server 422 message from
 *   ApiError.fields via form-errors.ts) rendered as React TEXT and wired
 *   to the control with aria-describedby + aria-invalid.
 * - Optional hint line, also described-by wired.
 *
 * The control is supplied by a render function so ANY input/select/
 * textarea gets the wiring without prop-forwarding guesswork.
 */
import type { ReactNode } from "react";
import styles from "./FormField.module.css";

export interface FieldControlProps {
  id: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
}

export function FormField({
  id,
  label,
  error,
  hint,
  children,
}: Readonly<{
  id: string;
  label: string;
  /** Field-level message; empty/undefined = no error state. */
  error?: string;
  hint?: string;
  children: (control: FieldControlProps) => ReactNode;
}>) {
  const hasError = error !== undefined && error !== "";
  const hintId = hint !== undefined ? `${id}-hint` : undefined;
  const errorId = hasError ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter((part) => part !== undefined).join(" ");

  const control: FieldControlProps = { id };
  if (hasError) control["aria-invalid"] = true;
  if (describedBy !== "") control["aria-describedby"] = describedBy;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      {children(control)}
      {hint !== undefined && (
        <div id={hintId} className={styles.hint}>
          {hint}
        </div>
      )}
      {hasError && (
        <div id={errorId} className={styles.error} role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
