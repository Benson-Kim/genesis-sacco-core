import type { ReactNode } from "react";
import styles from "./Field.module.css";
import formStyles from "./FormElements.module.css";

export interface FieldControlProps {
  id: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
}

export interface FieldProps {
  /** The `id` of the control; also sets `htmlFor` on the label. */
  htmlFor: string;
  label: string;
  /** Marks the label with a required asterisk. */
  required?: boolean;
  /** Field-level error message; presence also sets aria-invalid on the control. */
  error?: string;
  /** Helper text shown below the control when there is no error. */
  hint?: string;
  children: ((control: FieldControlProps) => ReactNode) | ReactNode;
}

/**
 * Form field row: persistent label above a control, with optional hint
 * and inline error. The control is supplied either as a render function
 * (receives id, aria-invalid, aria-describedby pre-wired) or as a plain
 * ReactNode when no ARIA wiring is needed.
 *
 * Error and hint styling come from FormElements.module.css (.error, .hint)
 * so they are consistent with any standalone use of those classes.
 */
export function Field({ htmlFor, label, required = false, error, hint, children }: FieldProps) {
  const hasError = error !== undefined && error !== "";
  const hintId = hint !== undefined ? `${htmlFor}-hint` : undefined;
  const errorId = hasError ? `${htmlFor}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ");

  const control: FieldControlProps = { id: htmlFor };
  if (hasError) control["aria-invalid"] = true;
  if (describedBy !== "") control["aria-describedby"] = describedBy;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={htmlFor}>
        {label}
        {required && <span className={formStyles.required} aria-hidden="true"> *</span>}
      </label>
      {typeof children === "function" ? children(control) : children}
      {hint !== undefined && !hasError && (
        <p id={hintId} className={formStyles.hint}>
          {hint}
        </p>
      )}
      {hasError && (
        <p id={errorId} className={formStyles.error} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
