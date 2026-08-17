import type { ReactNode } from "react";
import styles from "./Periods.module.css";

/** Labelled key/value detail row (the exits/loans drawer convention —
 * one module-local copy instead of one per file, gate 1.1). */
export function Kv({ label, children }: Readonly<{ label: string; children: ReactNode }>) {
  return (
    <div className={styles.kvRow}>
      <span className={styles.kvKey}>{label}</span>
      <span className={styles.kvVal}>{children}</span>
    </div>
  );
}
