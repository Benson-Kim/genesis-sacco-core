import type { ReactNode } from "react";
import styles from "./Field.module.css";

export interface FieldProps {
  label: string;
  htmlFor?: string;
  children: ReactNode;
}

/** Prototype `.field` form row: label above control. */
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
