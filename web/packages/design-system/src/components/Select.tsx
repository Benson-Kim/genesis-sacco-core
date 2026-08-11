import type { ReactNode, SelectHTMLAttributes } from "react";
import styles from "./FormElements.module.css";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  children: ReactNode;
}

/**
 * Styled native select. Wraps the `<select>` in `.selectWrap` so the
 * CSS chevron arrow is rendered, and applies the shared border/focus
 * styles from FormElements.module.css.
 */
export function Select({ className, children, ...rest }: SelectProps) {
  return (
    <div className={styles.selectWrap}>
      <select {...rest} className={[styles.formControl, className].filter(Boolean).join(" ")}>
        {children}
      </select>
    </div>
  );
}
