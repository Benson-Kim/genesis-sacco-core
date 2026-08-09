"use client";

/**
 * Accounting-periods register (the period machinery): the close-state read surface plus
 * the close-period entry point.
 *
 * Security posture (dividends/exits precedent):
 * - Every rendered string (ids, dates, statuses) is treated as
 *   attacker-influenced data; it renders exclusively through React
 *   text interpolation — no parser sink exists in this module.
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). The route itself is
 *   transactions:view; "Close period" mounts only with
 *   transactions:approve (the router's recorded decision: the people
 *   who post must not be able to freeze the periods they post into —
 *   deliberately NOT transactions:create/edit).
 * - Keyset pagination only (opaque cursors — scalability); the list
 *   contract declares NO filters — none are offered and nothing is
 *   filtered locally.
 * - HONESTY: rows exist only for months an approver CLOSED (design — absence of a row IS the open state); the register states
 *   this instead of fabricating synthetic "open" rows, and no reopen
 *   affordance exists because no reopen contract exists.
 * - NO money figure exists on this contract — nothing is summed,
 *   formatted or derived here (blocker (a) satisfied structurally).
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtDateTime } from "@/lib/format";
import { fetchPeriodsPage } from "../api";
import type { PeriodRecord } from "../schemas";
import { periodStatusPill } from "./pills";
import styles from "./Periods.module.css";

// Dialog-level code splitting (speed): the close dialog
// chunk loads on first open, not with the list route.
const ClosePeriodDialog = dynamic(
  () => import("./ClosePeriodDialog").then((m) => m.ClosePeriodDialog),
  { ssr: false },
);

export function PeriodsScreen() {
  const permissions = usePermissions();
  const [closing, setClosing] = useState(false);

  const list = useKeysetList<PeriodRecord>({
    queryKey: ["periods", "list"],
    fetchPage: (cursor) => fetchPeriodsPage(cursor),
  });

  // Closing the books is the transactions-APPROVE control (P4 matrix,
  // recorded on the router) — a control over every posting, held by
  // the approve holders, never the posting roles.
  const mayClose = can(permissions.data, "transactions", "approve");

  const columns: Column<PeriodRecord>[] = [
    {
      key: "bounds",
      header: "Period (calendar month)",
      render: (period) => (
        // Calendar dates verbatim (date.isoformat()) — not instants;
        // fmtDateTime would invent a time of day for them.
        <span>
          {period.period_start} → {period.period_end}
        </span>
      ),
    },
    {
      key: "status",
      header: "Close state",
      render: (period) => periodStatusPill(period.status),
    },
    {
      key: "closed_at",
      header: "Closed at",
      render: (period) => (
        <span className={styles.muted}>
          {period.closed_at === null ? "—" : fmtDateTime(period.closed_at)}
        </span>
      ),
    },
  ];

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.toolbarNote}>
          A row exists only for a month the books were CLOSED for — every
          other month is open for posting (posting dates are resolved and
          checked server-side; the database trigger refuses
          postings into a closed month regardless of any screen). There is
          no reopen: a close is a governance fact, not a toggle.
        </div>
        <div className={styles.toolbarActions}>
          {mayClose && (
            <Button type="button" variant="primary" onClick={() => setClosing(true)}>
              Close period…
            </Button>
          )}
        </div>
      </div>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Closed accounting periods</span>
          <span className={styles.registerNote}>Newest first · server close-stamps verbatim</span>
        </div>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(period) => period.id}
          emptyMessage="No accounting period has been closed yet — every month is open for posting."
        />
      </Card>

      {closing && <ClosePeriodDialog onClose={() => setClosing(false)} />}
    </div>
  );
}
