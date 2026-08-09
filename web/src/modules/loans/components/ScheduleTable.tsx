"use client";

/**
 * Server-computed installment schedule, rendered VERBATIM (blocker
 * (a)). One copy shared by the loan detail drawer and the disbursement
 * result panel (reuse-first).
 */
import { fmtKes } from "@/lib/format";
import type { ScheduleRow } from "../schemas";
import styles from "./Loans.module.css";

export function ScheduleTable({ rows }: Readonly<{ rows: ScheduleRow[] }>) {
  if (rows.length === 0) {
    return <div className={styles.formNote}>No schedule rows returned for this loan.</div>;
  }
  return (
    <div className={styles.miniTableWrap}>
      <table className={styles.miniTable}>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Due date</th>
            <th scope="col" className={styles.num}>
              Principal due
            </th>
            <th scope="col" className={styles.num}>
              Interest due
            </th>
            <th scope="col" className={styles.num}>
              Total due
            </th>
            <th scope="col" className={styles.num}>
              Paid
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.installment_no}>
              <td>{row.installment_no}</td>
              <td>{row.due_date}</td>
              <td className={styles.num}>{fmtKes(row.principal_due)}</td>
              <td className={styles.num}>{fmtKes(row.interest_due)}</td>
              <td className={styles.num}>{fmtKes(row.total_due)}</td>
              <td className={styles.num}>{fmtKes(row.paid_amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
