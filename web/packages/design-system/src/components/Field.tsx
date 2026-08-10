import type { ReactNode } from "react";
import styles from "./Field.module.css";

export interface FieldProps {
  label: string;
  htmlFor?: string;
  children: ReactNode;
}

/**
 * `.field` form row: label above control.
 *
 * Internal composition primitive only — not exported from the package
 * index. Application forms use the FormField render-prop wrapper
 * (`web/src/modules/forms/FormField`), which owns error/aria wiring.
 */
export function Field({ label, htmlFor, children }: FieldProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={htmlFor}>
        {label}
      </label>
      {children}
    </div>
  );
}
