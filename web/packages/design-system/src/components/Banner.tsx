import type { ReactNode } from "react";
import styles from "./Banner.module.css";

export interface BannerProps {
  children: ReactNode;
  /** Prototype banner variants: info (navy), error (brick), ok (emerald). */
  variant?: "info" | "error" | "ok";
  className?: string;
}

/**
 * Inline operator notice. Content renders as React children only — never
 * raw markup — so attacker-influenced strings (names, correlation ids)
 * stay inert text (gate 1.6).
 */
export function Banner({ children, variant = "info", className }: Readonly<BannerProps>) {
  const classes = [styles.banner, styles[variant], className].filter(Boolean).join(" ");
  return (
    <div className={classes} role={variant === "error" ? "alert" : undefined}>
      {children}
    </div>
  );
}
