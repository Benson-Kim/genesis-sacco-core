import type { TextareaHTMLAttributes } from "react";
import styles from "./FormElements.module.css";

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

/**
 * Styled textarea. Applies `.formControl` from FormElements.module.css;
 * the `textarea.formControl` rule sets `min-height: 8rem; resize: vertical`.
 */
export function Textarea({ className, ...rest }: TextareaProps) {
  const classes = [styles.formControl, className].filter(Boolean).join(" ");
  return <textarea {...rest} className={classes} />;
}
