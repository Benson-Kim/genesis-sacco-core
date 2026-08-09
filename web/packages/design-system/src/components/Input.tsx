import type { InputHTMLAttributes } from "react";
import styles from "./FormElements.module.css";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Apply the `.otpInput` modifier for monospace single-code fields. */
  otp?: boolean;
}

/**
 * Styled text input. Applies `.formControl` from FormElements.module.css.
 * Pass `otp` to additionally apply the `.otpInput` modifier for a
 * monospace, letter-spaced single-field OTP input.
 */
export function Input({ otp = false, className, ...rest }: InputProps) {
  const classes = [styles.formControl, otp ? styles.otpInput : undefined, className]
    .filter(Boolean)
    .join(" ");
  return <input {...rest} className={classes} />;
}
