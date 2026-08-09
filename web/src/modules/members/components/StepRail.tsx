/**
 * Registration step rail — the design flow's four steps as ONE
 * code-owned list rendered by BOTH registration surfaces (the core
 * identity drawer at step 1 and the KYC wizard at steps 2–4), so the
 * operator always sees where they are in the same flow. Falsifiable:
 * reorder or rename a step and the step-order leg fails in both
 * surfaces at once.
 */
import styles from "./Members.module.css";

export const REGISTRATION_STEPS = [
  "Member identity",
  "KYC details",
  "Membership & consent",
  "Documents & review",
] as const;

export function StepRail({ current }: Readonly<{ current: number }>) {
  return (
    <ol className={styles.stepRail} aria-label="Registration steps">
      {REGISTRATION_STEPS.map((label, index) => (
        <li
          key={label}
          className={index === current ? styles.stepCurrent : styles.step}
          aria-current={index === current ? "step" : undefined}
        >
          {index < current ? "✓ " : `${index + 1}. `}
          {label}
        </li>
      ))}
    </ol>
  );
}
