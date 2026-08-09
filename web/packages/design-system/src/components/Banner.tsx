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
 * stay inert text (least disclosure).
 *
 * Live-region convention (W56-4): `error` banners are
 * assertive alerts; `info`/`ok` notices carry `role="status"` (polite
 * live region) so conflict-withdrawal and reload notices reach
 * assistive technology without a separate announce() call. All
 * variants read atomically (partial reads are worse than silence).
 */
export function Banner({ children, variant = "info", className }: Readonly<BannerProps>) {
    const classes = [styles.banner, styles[variant], className].filter(Boolean).join(" ");
    return (
        <div
            className={classes}
            role={variant === "error" ? "alert" : "status"}
            aria-atomic="true"
        >
            {children}
        </div>
    );
}

