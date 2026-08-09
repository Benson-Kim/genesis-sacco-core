"use client";

/**
 * Member-exit register (module 7 — prototype member exit, adapted to the contract): the exit workflow's read surface plus entry
 * points for the lifecycle writes.
 *
 * Security posture (transactions/loans precedent):
 * - Every rendered string (ids, reasons) is attacker-influenced data;
 *   it renders exclusively through React text interpolation — no
 *   parser sink exists in this module (gate-tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). "Request exit" mounts
 *   only with members:edit (the route itself is members:view via
 *   RequireModule); vote/void/settle affordances mount in the drawers
 *   only with members:approve.
 * - Keyset pagination only (opaque cursors — scalability); the status
 *   filter — the ONLY filter the list contract declares — is a
 *   SERVER query parameter; nothing is filtered locally.
 * - MONEY (blocker (a)): reports picker consumes this list with
 *   its money fields STRIPPED; THIS register is where those figures
 *   render — each snapshot component (shares, deposits, loan balance,
 *   fees, net payable) is an API decimal STRING rendered via fmtKes in
 *   its own labelled column, never summed, netted or re-derived (no
 *   totals row exists; a negative net payable renders verbatim).
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { EMPTY_EXIT_FILTERS, fetchExitsPage, type ExitListFilters } from "../api";
import { EXIT_STATUSES, EXIT_STATUS_LABELS, type ExitRecord } from "../schemas";
import { exitStatusPill } from "./pills";
import { FormField } from "@/modules/forms/FormField";
import styles from "./Exits.module.css";

// Drawer-level code splitting (speed): drawer chunks load
// on first open, not with the list route.
const ExitDetailDrawer = dynamic(
  () => import("./ExitDetailDrawer").then((m) => m.ExitDetailDrawer),
  { ssr: false },
);
const RequestExitDrawer = dynamic(
  () => import("./RequestExitDrawer").then((m) => m.RequestExitDrawer),
  { ssr: false },
);
const SettleExitDialog = dynamic(
  () => import("./SettleExitDialog").then((m) => m.SettleExitDialog),
  { ssr: false },
);
const ExitStatementDrawer = dynamic(
  () => import("./ExitStatementDrawer").then((m) => m.ExitStatementDrawer),
  { ssr: false },
);

type DrawerState =
  | null
  | { mode: "request" }
  | { mode: "detail"; exitId: string }
  | { mode: "settle"; exitId: string }
  | { mode: "statement"; exitId: string };

export function ExitsScreen() {
  const permissions = usePermissions();
  const [filters, setFilters] = useState<ExitListFilters>(EMPTY_EXIT_FILTERS);
  const [drawer, setDrawer] = useState<DrawerState>(null);

  const list = useKeysetList<ExitRecord>({
    queryKey: ["exits", "list", filters],
    fetchPage: (cursor) => fetchExitsPage(filters, cursor),
  });

  // Creating an exit request is a member lifecycle change
  // (members:edit); the drawer also fresh-reads the member — the same
  // module grant the route guard already established.
  const mayRequest = can(permissions.data, "members", "edit");

  const columns: Column<ExitRecord>[] = [
    {
      key: "requested",
      header: "Requested",
      render: (exit) => <span className={styles.muted}>{fmtDateTime(exit.created_at)}</span>,
    },
    {
      key: "member",
      header: "Member",
      render: (exit) => (
        // The list carries member_id only (no joined name) — the
        // detail drawer resolves the member record (precedent).
        <span className={styles.mono} title={exit.member_id}>
          {exit.member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (exit) => exitStatusPill(exit.status),
    },
    {
      key: "shares",
      header: "Shares",
      align: "right",
      render: (exit) => <span className={styles.amountCell}>{fmtKes(exit.shares_amount)}</span>,
    },
    {
      key: "deposits",
      header: "Deposits",
      align: "right",
      render: (exit) => <span className={styles.amountCell}>{fmtKes(exit.deposits_amount)}</span>,
    },
    {
      key: "loan_balance",
      header: "Loan balance",
      align: "right",
      render: (exit) => <span className={styles.amountCell}>{fmtKes(exit.loan_balance)}</span>,
    },
    {
      key: "fees",
      header: "Fees",
      align: "right",
      render: (exit) => <span className={styles.amountCell}>{fmtKes(exit.fees)}</span>,
    },
    {
      key: "net_payable",
      header: "Net payable",
      align: "right",
      render: (exit) => <span className={styles.netCell}>{fmtKes(exit.net_payable)}</span>,
    },
  ];

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <FormField id="exit-filter-status" label="Status">
            {(control) => (
              <select
                {...control}
                className={`${styles.select} ${styles.filterControl}`}
                value={filters.status}
                onChange={(event) =>
                  setFilters({ status: event.target.value as ExitListFilters["status"] })
                }
              >
                <option value="">All statuses</option>
                {EXIT_STATUSES.map((option) => (
                  <option key={option} value={option}>
                    {EXIT_STATUS_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
        </div>
        <div className={styles.toolbarActions}>
          {mayRequest && (
            <Button
              type="button"
              variant="primary"
              onClick={() => setDrawer({ mode: "request" })}
            >
              + Request exit
            </Button>
          )}
        </div>
      </div>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Member exit register</span>
          <span className={styles.registerNote}>
            Settlement snapshots are server-computed under locks — figures
            render verbatim; totals are not computed in this screen
          </span>
        </div>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(exit) => exit.id}
          emptyMessage="No exit requests match this filter."
          onRowClick={(exit) => setDrawer({ mode: "detail", exitId: exit.id })}
        />
      </Card>

      {drawer !== null && drawer.mode === "request" && (
        <RequestExitDrawer onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "detail" && (
        <ExitDetailDrawer
          exitId={drawer.exitId}
          onSettle={(record) => setDrawer({ mode: "settle", exitId: record.id })}
          onViewStatement={() => setDrawer({ mode: "statement", exitId: drawer.exitId })}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "settle" && (
        <SettleExitDialog exitId={drawer.exitId} onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "statement" && (
        <ExitStatementDrawer exitId={drawer.exitId} onClose={() => setDrawer(null)} />
      )}
    </div>
  );
}
