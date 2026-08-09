import type { ReactNode } from "react";
import { Required } from "./FormElements.module";
import styles from "./Field.module.css";

export interface FieldProps {
  label: string;
  htmlFor?: string;
  /** Shows the shared `<Required>` asterisk beside the label. Doesn't add
   * `required`/`aria-invalid` to the control itself — set those on the
   * control (e.g. `<FormControl required>`) so the semantics live with
   * the element they actually describe. */
  required?: boolean;
  children: ReactNode;
}

/** Prototype `.field` form row: label above control. */
export function Field({ label, htmlFor, required, children }: FieldProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={htmlFor}>
        {label}
        {required === true && <Required />}
      </label>
      {children}
    </div>
  );
}