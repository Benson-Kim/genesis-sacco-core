"use client";

/**
 * Exit-statement drawer (module 7): the CANONICAL exit
 * statement JSON document (`GET /member-exits/{id}/statement`,
 * members:view) rendered VERBATIM — a financial document, not a
 * computed view.
 *
 * - EVERY figure and line is the SERVER's (blocker (a)): shares,
 *   deposits, the server-computed equity line, loan balance, fees and
 *   net payable render via fmtKes exactly as received — nothing is
 *   summed, netted or re-derived (the client never checks that equity
 *   equals shares plus deposits; that arithmetic is the server's).
 * - Every string field (member name, member no, reason, txn ref) is
 *   attacker-influenced data and renders exclusively as React text —
 *   no parser sink exists in this module (gate-tested, the posture extended to statement fields).
 * - READ-ONLY surface: no lifecycle write mounts here and the default
 *   light overlay dismissal applies (the W56-3 convention reserves the
 *   opt-out for form-bearing surfaces). No download affordance exists
 *   here — the CSV/PDF export of this document is the reports module's
 *   `member_exit_statement` (blocker (k)), so no filename is ever
 *   derived from a server string (the class cannot arise).
 */
import { useQuery } from "@tanstack/react-query";
import { Kv, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchExitStatement } from "../api";
import { EXIT_STATUS_LABELS } from "../schemas";
import { exitStatusPill } from "./pills";
import styles from "./Exits.module.css";

export function ExitStatementDrawer({
  exitId,
  onClose,
}: Readonly<{
  exitId: string;
  onClose: () => void;
}>) {
  const statement = useQuery({
    queryKey: ["exits", "statement", exitId],
    queryFn: () => fetchExitStatement(exitId),
    // Record class: the statement reflects the record's CURRENT state
    // (it changes on approval/settlement) — never served stale.
    staleTime: STALE_TIME.record,
  });

  return (
    <Modal title="Exit statement" onClose={onClose}>
      {statement.isPending && <div className={styles.muted}>Loading statement…</div>}
      {statement.isError && <ErrorBanner error={statement.error} />}
      {statement.data !== undefined && (
        <div className={styles.statementDoc}>
          <div className={styles.statementHead}>
            Member exit statement — {statement.data.member_name} ·{" "}
            {statement.data.member_no}
          </div>
          <div className={styles.statementSub}>
            Exit record <span className={styles.mono}>{statement.data.exit_id}</span> · member
            record <span className={styles.mono}>{statement.data.member_id}</span>
          </div>
          <Kv label="Exit status">{exitStatusPill(statement.data.exit_status)}</Kv>
          <Kv label="Member status">{statement.data.member_status}</Kv>
          <Kv label="Reason">{statement.data.reason ?? "—"}</Kv>
          <Kv label="Requested by">
            {/* Initiator attribution on the canonical document (/ follow-up): the SERVER's bare staff UUID via
                the short-id convention — least disclosure: no
                name/email is ever fetched for it; NULL renders the
                honest unattributed line. */}
            {statement.data.requested_by !== null ? (
              <span className={styles.mono} title={statement.data.requested_by}>
                {statement.data.requested_by.slice(0, 8)}
              </span>
            ) : (
              "— (unattributed)"
            )}
          </Kv>

          <div className={styles.subhead}>Settlement figures (server document)</div>
          <Kv label="Share capital">{fmtKes(statement.data.shares_amount)}</Kv>
          <Kv label="Deposits">{fmtKes(statement.data.deposits_amount)}</Kv>
          <Kv label="Equity (server-computed)">{fmtKes(statement.data.equity)}</Kv>
          <Kv label="Loan balance (offset)">{fmtKes(statement.data.loan_balance)}</Kv>
          <Kv label="Exit fees (tenant-configured)">{fmtKes(statement.data.fees)}</Kv>
          <Kv label="Net payable">
            <span className={styles.netCell}>{fmtKes(statement.data.net_payable)}</span>
          </Kv>

          <div className={styles.subhead}>Timeline</div>
          <Kv label="Requested">{fmtDateTime(statement.data.requested_at)}</Kv>
          <Kv label="Decided">{fmtDateTime(statement.data.decided_at)}</Kv>
          <Kv label="Settled">{fmtDateTime(statement.data.settled_at)}</Kv>
          <Kv label="Settlement txn ref">
            {statement.data.settlement_txn_ref !== null ? (
              <span className={styles.mono}>{statement.data.settlement_txn_ref}</span>
            ) : (
              "—"
            )}
          </Kv>

          <div className={styles.formNote}>
            This is the canonical P12 statement document, rendered line-by-line
            exactly as the server returned it (
            {EXIT_STATUS_LABELS[statement.data.exit_status]} state) — no figure
            was computed in this screen. A CSV/PDF copy is produced from
            Reports ▸ Member exit statement.
          </div>
        </div>
      )}
    </Modal>
  );
}
