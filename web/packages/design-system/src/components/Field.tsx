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

/**
 * `.field` form row: label above control.
 *
 * Internal composition primitive only — not exported from the package
 * index. Application forms use the FormField render-prop wrapper
 * (`web/src/modules/forms/FormField`), which owns error/aria wiring.
 */
export function Field({ label, htmlFor, required, children }: FieldProps) {
  return (
    <div className={styles.field}>
      {/* The aria-hidden asterisk lives OUTSIDE the <label> element so the
          label's accessible text stays exactly `label` (testing-library /
          AT label resolution); the wrapper span keeps the flex visuals. */}
      <span className={styles.labelRow}>
        <label className={styles.label} htmlFor={htmlFor}>
          {label}
        </label>
        {required === true && <Required />}
      </span>
      {children}
    </div>
  );
}