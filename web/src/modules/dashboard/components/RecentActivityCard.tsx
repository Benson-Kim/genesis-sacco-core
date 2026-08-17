"use client";

/**
 * "Recent activity" ledger strip — the prototype's dashboard affordance:
 * a Recent activity heading with a "View ledger ›" link to the full
 * register, over the newest postings (date+time, member, type, channel,
 * signed amount).
 *
 * Security posture:
 * - Mounts ONLY for holders of transactions:view — the same grant the
 *   register itself requires. A stripped role renders nothing AND
 *   fetches nothing (the useKeysetList/MemberFilter precedent): the
 *   dashboard must not become a side door to money rows.
 * - MONEY: every figure is the SERVER's decimal string rendered through
 *   fmtAmount. The sign comes from the posting's own `direction` — the
 *   client never nets, sums, or re-derives a figure (blocker (a)).
 * - Colour is never the only signal: the sign character (+/-) carries
 *   the same information as the emerald/brick treatment.
 * - Every rendered string is attacker-influenced data and goes through
 *   React text interpolation only (the named XSS threat, gate-tested).
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Card, grid } from "@genesis/design-system";
import { fmtAmount, fmtDateTimeParts } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchTransactionsPage, EMPTY_TXN_FILTERS } from "@/modules/transactions/api";
import { CHANNEL_LABELS, type Transaction } from "@/modules/transactions/schemas";
import { txnTypePill } from "@/modules/transactions/components/pills";
import styles from "./DashboardScreen.module.css";

/** Bounded strip, not a register: the dashboard shows the newest few
 * postings and links out for the rest (the full walk is the register's
 * keyset job, never an unbounded dashboard read). */
const RECENT_LIMIT = 8;

export function RecentActivityCard() {
  const recent = useQuery({
    queryKey: ["dashboard", "recent-activity", RECENT_LIMIT],
    // The register's own keyset reader, consumed unmodified
    // (reuse-first): server order, no local sorting or filtering.
    queryFn: () => fetchTransactionsPage(EMPTY_TXN_FILTERS, null, RECENT_LIMIT),
    staleTime: STALE_TIME.list,
  });

  const rows: Transaction[] = recent.data?.items ?? [];

  return (
    <Card padded={false} className={grid.wide}>
      <div className={styles.activityHead}>
        <h2 className={styles.title}>Recent activity</h2>
        <Link className={styles.viewLedger} href="/modules/transactions">
          View ledger ›
        </Link>
      </div>
      <table className={styles.flows}>
        <thead>
          <tr>
            <th className={styles.th} scope="col">
              Date
            </th>
            <th className={styles.th} scope="col">
              Member
            </th>
            <th className={styles.th} scope="col">
              Type
            </th>
            <th className={styles.th} scope="col">
              Channel
            </th>
            <th className={styles.thRight} scope="col">
              Amount (KES)
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((txn) => {
            const { date, time } = fmtDateTimeParts(txn.occurred_at);
            const isDebit = txn.direction === "debit";
            return (
              <tr key={txn.id}>
                <td className={styles.td}>
                  <div className={styles.dateTime}>
                    <span className={styles.date}>{date}</span>
                    {time && <span className={styles.time}>{time}</span>}
                  </div>
                </td>
                <td className={styles.td}>
                  {txn.member_no !== null ? (
                    <div>
                      <div className={styles.cellStrong}>{txn.member_name}</div>
                      <div className={styles.cellSub}>{txn.member_no}</div>
                    </div>
                  ) : (
                    // Never a raw uuid fallback — honest text instead.
                    <span className={styles.muted}>Unresolved member</span>
                  )}
                </td>
                <td className={styles.td}>{txnTypePill(txn.type, txn.direction)}</td>
                <td className={styles.td}>{CHANNEL_LABELS[txn.channel]}</td>
                <td
                  className={`${styles.tdRight} ${isDebit ? styles.amountDebit : styles.amountCredit}`}
                >
                  {/* The sign is the DIRECTION the server reported — the
                      magnitude is its verbatim string, never negated or
                      re-derived client-side. */}
                  {isDebit ? "-" : "+"}
                  {fmtAmount(txn.amount)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && !recent.isPending && (
        <div className={styles.activityEmpty}>No postings yet.</div>
      )}
    </Card>
  );
}
