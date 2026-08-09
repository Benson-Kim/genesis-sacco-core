"use client";

/**
 * Transaction detail drawer (prototype `vTxn` row
 * drill-down). The P11 contract exposes NO per-transaction read
 * endpoint — this drawer renders the WITNESSED list row: an
 * append-only ledger fact that never changes after it is served
 * (corrections post reversing rows; they never update this one).
 *
 * - EVERY figure renders VERBATIM from the API decimal string
 *   (blocker (a)). The list model carries no running balance and none
 *   is reconstructed here — balances are server facts that live on
 *   write responses and statements (honest limitation in the MR).
 * - The row carries `member_id` only (pattern (b)); the member name
 *   resolves via GET /members/{id}, gated on members:view
 *   (deny-by-default — a role without the grant fetches NOTHING and
 *   sees the opaque id).
 * - Every rendered string (refs, member names) is attacker-influenced
 *   data; it renders exclusively through React text interpolation.
 */
import { useQuery } from "@tanstack/react-query";
import { Banner, Kv, Modal } from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import { fetchTransactionLegs } from "../api";
import { CHANNEL_LABELS, SIDE_LABELS, type Transaction } from "../schemas";
import { directionPill, reversalPill, txnTypePill } from "./pills";
import styles from "./Transactions.module.css";

export function TransactionDetailDrawer({
  txn,
  onClose,
}: Readonly<{
  txn: Transaction;
  onClose: () => void;
}>) {
  const permissions = usePermissions();
  const mayViewMembers = can(permissions.data, "members", "view");
  const mayViewTransactions = can(permissions.data, "transactions", "view");

  // Double-entry legs drill-down: the append-only
  // ledger_entries truth behind this row, one item per DR/CR leg.
  // Structurally withheld without transactions:view (deny-by-default:
  // no grant, no probe — least disclosure); ledger rows are immutable server
  // facts, so the register staleness class applies.
  const legs = useQuery({
    queryKey: ["transactions", "legs", txn.id],
    queryFn: () => fetchTransactionLegs(txn.id),
    enabled: mayViewTransactions,
    staleTime: STALE_TIME.record,
  });

  const memberId = txn.member_id;
  const member = useQuery({
    queryKey: ["members", "detail", memberId ?? "none"],
    queryFn: () => {
      if (memberId === null) return Promise.reject(new Error("no member on this row"));
      return fetchMember(memberId);
    },
    // Deny-by-default: without members:view this drawer fetches NOTHING
    // and renders the opaque id (least disclosure).
    enabled: memberId !== null && mayViewMembers,
    staleTime: STALE_TIME.record,
  });

  return (
    <Modal title="Transaction detail" onClose={onClose}>
      {txn.is_reversal && (
        <Banner>
          This is a REVERSING entry: the append-only ledger corrects by
          posting a reversal, never by editing the original row.
        </Banner>
      )}
      <div className={styles.detailGrid}>
        <Kv label="Reference">
          <span className={styles.mono}>{txn.txn_ref}</span>
        </Kv>
        <Kv label="External reference">
          {txn.external_ref === null ? (
            <span className={styles.muted}>— (system posting or pre-0043 row)</span>
          ) : (
            <span className={styles.mono}>{txn.external_ref}</span>
          )}
        </Kv>
        <Kv label="Type">{txnTypePill(txn.type, txn.direction)}</Kv>
        <Kv label="Direction">{directionPill(txn.direction)}</Kv>
        <Kv label={`Amount (${SIDE_LABELS[txn.direction]})`}>{fmtKes(txn.amount)}</Kv>
        <Kv label="Channel">{CHANNEL_LABELS[txn.channel]}</Kv>
        <Kv label="Occurred">{fmtDateTime(txn.occurred_at)}</Kv>
        <Kv label="Member">
          {memberId === null ? (
            "— (tenant-level posting)"
          ) : member.data !== undefined ? (
            <>
              {member.data.name} · {member.data.member_no}
            </>
          ) : (
            <span className={styles.mono}>{memberId}</span>
          )}
        </Kv>
        <Kv label="Reversal">{txn.is_reversal ? reversalPill() : "No"}</Kv>
        <Kv label="Posted by">
          {/* Posting-actor attribution:
              the SERVER's bare staff UUID, short-id convention — least
              disclosure: no name/email is ever fetched for it.
              NULL renders the honest affordance (FM-B): system/job
              postings and pre-0036 rows are never given an invented
              actor. */}
          {txn.created_by !== null ? (
            <span className={styles.mono} title={txn.created_by}>
              {txn.created_by.slice(0, 8)}
            </span>
          ) : (
            "— (system posting or unattributed)"
          )}
        </Kv>
        <Kv label="Ledger row id">
          <span className={styles.mono}>{txn.id}</span>
        </Kv>
      </div>

      {/* Double-entry legs: the per-posting DR/CR
          rows from the append-only ledger_entries truth, rendered
          VERBATIM — never summed, never netted, no client-side
          balancing total (blocker (a)); the 0004/0014 DB trigger
          proved balance at commit time. Structurally withheld without
          transactions:view. */}
      {mayViewTransactions && (
        <div>
          <div className={styles.subhead}>Double-entry legs</div>
          {legs.isPending && <div className={styles.muted}>Loading legs…</div>}
          {legs.isError && <ErrorBanner error={legs.error} />}
          {legs.data !== undefined && (
            <table className={styles.legsTable}>
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Side</th>
                  <th scope="col" className={styles.legsAmountHead}>
                    Amount
                  </th>
                </tr>
              </thead>
              <tbody>
                {legs.data.map((leg, index) => (
                  <tr key={`${leg.account}-${leg.side}-${index}`}>
                    <td>
                      {/* The account key is server data rendered as
                          inert React text (XSS-tested). */}
                      <span className={styles.mono}>{leg.account}</span>
                    </td>
                    <td>{directionPill(leg.side)}</td>
                    <td className={leg.side === "debit" ? styles.drCell : styles.crCell}>
                      {fmtKes(leg.amount)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className={styles.formNote}>
            One row per ledger leg, verbatim from the server — debits and
            credits are never summed or netted here: the database proved the
            posting balanced when it committed.
          </div>
        </div>
      )}

      <div className={styles.formNote}>
        Ledger rows are immutable server facts. This contract serves no
        per-row running balance — account balances appear on posting
        responses and member statements, and are never reconstructed in
        this screen.
      </div>
    </Modal>
  );
}
