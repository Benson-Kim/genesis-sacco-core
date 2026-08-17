import type { ReactNode } from "react";
import { cssVar, type ColorToken } from "../tokens";
import styles from "./Pill.module.css";

export interface PillProps {
  children: ReactNode;
  /** Background token, e.g. "emeraldSoft" (prototype status pills). */
  bg?: ColorToken;
  /** Text token, e.g. "emerald". */
  color?: ColorToken;
}

export function Pill({ children, bg = "panel", color = "sub" }: Readonly<PillProps>) {
  return (
    <span
      className={styles.pill}
      style={{ background: cssVar(bg), color: cssVar(color) }}
    >
      {children}
    </span>
  );
}
