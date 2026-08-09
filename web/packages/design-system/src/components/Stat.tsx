import type { ReactNode } from "react";
import styles from "./Stat.module.css";

export interface StatProps {
  label: string;
  value: ReactNode;
}

/** Prototype `.stat` KPI block (label above value). */
export function Stat({ label, value }: StatProps) {
  return (
    <div>
      <div className={styles.label}>{label}</div>
      <div className={styles.value}>{value}</div>
    </div>
  );
}
