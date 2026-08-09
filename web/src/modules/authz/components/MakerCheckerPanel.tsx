"use client";

/**
 * Maker-checker approval panel (security primitive — one copy, reuse-first, for every module with an approval step: applications, committee, transactions, member exit…).
 *
 * STRUCTURAL self-approval impossibility (the maker-checker posture, UX side): the checker affordances are passed in as
 * `checkerActions`, and this panel MOUNTS THEM ONLY when the signed-in
 * user is a DIFFERENT, KNOWN principal from the maker:
 *   - own id unknown (no session claim) => deny by default, no actions;
 *   - own id === maker id => SoD banner, no actions.
 * There is no prop to override this — a screen cannot even offer
 * self-approval by mistake. The server enforces regardless (least disclosure);
 * this panel exists so the UI never offers what the API forbids.
 *
 * Maker identity fields are attacker-influenced data and render as React
 * text only.
 */
import type { ReactNode } from "react";
import { Banner } from "@genesis/design-system";
import { getOwnUserId } from "@/modules/auth/session";
import styles from "./MakerCheckerPanel.module.css";

export interface MakerCheckerPanelProps {
  /** The maker's user id (the principal that initiated the action). */
  makerId: string;
  /** Operator-facing maker label (name/email) — rendered as text. */
  makerLabel: string;
  /** When the maker acted (pre-formatted display string). */
  makerAt?: string;
  /** What is awaiting the check — rendered inside the maker context. */
  children?: ReactNode;
  /** Approve/reject affordances — mounted ONLY for an eligible checker. */
  checkerActions: ReactNode;
}

export function MakerCheckerPanel({
  makerId,
  makerLabel,
  makerAt,
  children,
  checkerActions,
}: Readonly<MakerCheckerPanelProps>) {
  const ownId = getOwnUserId();
  const isSelf = ownId !== null && ownId === makerId;
  const eligibleChecker = ownId !== null && ownId !== makerId;

  return (
    <div className={styles.panel}>
      <section className={styles.maker} aria-label="Maker">
        <div className={styles.roleTag}>Maker</div>
        <div className={styles.makerIdentity}>{makerLabel}</div>
        {makerAt !== undefined && <div className={styles.makerAt}>{makerAt}</div>}
        {children}
      </section>
      <section className={styles.checker} aria-label="Checker">
        <div className={styles.roleTag}>Checker</div>
        {isSelf && (
          <Banner>
            You initiated this action. Approval requires a DIFFERENT
            administrator (separation of duties) — your own approval controls
            are never offered.
          </Banner>
        )}
        {!isSelf && !eligibleChecker && (
          <Banner>
            Checker identity could not be established — approval controls are
            withheld (deny by default).
          </Banner>
        )}
        {eligibleChecker && checkerActions}
      </section>
    </div>
  );
}
