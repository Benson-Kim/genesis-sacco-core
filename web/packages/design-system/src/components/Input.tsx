import type { InputHTMLAttributes } from "react";
import styles from "./FormElements.module.css";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Apply the `.otpInput` modifier for monospace single-code fields. */
  otp?: boolean;
  /**
   * Skip `.formControl` base styles entirely and rely only on `className`.
   * Use when the call site owns all sizing/border styling (e.g. OTP digit boxes).
   */
  unstyled?: boolean;
}

/**
 * Styled text input. Applies `.formControl` from FormElements.module.css.
 * Pass `otp` to additionally apply the `.otpInput` modifier for a
 * monospace, letter-spaced single-field OTP input.
 * Pass `unstyled` to skip `.formControl` and rely solely on `className`.
 */
export function Input({ otp = false, unstyled = false, className, ...rest }: InputProps) {
  const classes = [
    unstyled ? undefined : styles.formControl,
    otp ? styles.otpInput : undefined,
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return <input {...rest} className={classes} />;
}
