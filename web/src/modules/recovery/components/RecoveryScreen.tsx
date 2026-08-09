"use client";

/**
 * Recovery worklist (follow-on, —; the recovery-officer worklist the prototype's "Initiate recovery" promised): open cases, most delinquent first —
 * the server's order, never re-sorted locally.
 *
 * Security posture:
 * - Route guard is loan_book:view (RequireModule — the read grant); "Initiate recovery" mounts only with loan_book:create;
 *   case writes mount in the drawer only with loan_book:edit. Pure UX;
 *   the server enforces every call (least disclosure).
 * - LEAST DISCLOSURE (addendum): the worklist exposes workflow
 *   facts, days-past-due and the classification pill ONLY — NO
 *   balance, penalty or provision column exists on the contract or
 *   this screen; the loan's money lives behind the Loan book, for the
 *   entitled.
 * - Keyset pagination only (opaque cursors — scalability). The worklist
 *   offers exactly the contract's two DECLARED filters (the human-authorized read-contract expansion):
 *   status (one LIVE posture, segmented control) and classification
 *   (one stored LoanClass label, labelled select — 5 values + All
 *   exceeds the ≤5 segment rule). Every value is a code-owned
 *   vocabulary member bound server-side; the server keeps its
 *   most-delinquent-first default order and NOTHING is filtered or
 *   re-sorted locally.
 * - Staff UUIDs (assignees) render via the short-id convention;
 *   NULL is the honest "unassigned"; a suspended-after-assignment
 *   officer renders the honest flag pill (assignee_unassignable) — the
 *   case is never silently orphaned.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card, Pill } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { fmtDateTime } from "@/lib/format";
import { loanClassPill } from "@/modules/loans/components/pills";
import { LOAN_CLASS_LABELS } from "@/modules/loans/schemas";
import { fetchWorklistPage, type WorklistFilters } from "../api";
import {
  CASE_STATUS_LABELS,
  WORKLIST_CLASSES,
  WORKLIST_STATUS_FILTERS,
  type WorklistClass,
  type WorklistRow,
  type WorklistStatusFilter,
} from "../schemas";
import { FormField } from "@/modules/forms/FormField";
import styles from "./Recovery.module.css";

// Drawer-level code splitting (speed).
const OpenCaseDrawer = dynamic(() => import("./OpenCaseDrawer").then((m) => m.OpenCaseDrawer), {
  ssr: false,
});
const CaseDetailDrawer = dynamic(
  () => import("./CaseDetailDrawer").then((m) => m.CaseDetailDrawer),
  { ssr: false },
);

type DrawerState = null | { mode: "open" } | { mode: "detail"; caseId: string };

/** The two PAUSE postures as explicit segments; the default segment
 * sends NO status param at all — the server's as-built default (OPEN
 * cases) is preserved byte-identically, never re-requested as an
 * explicit status=open (the declared value stays contract-valid and
 * is exercised by the network suite). ≤5 options per. */
const STATUS_SEGMENTS = WORKLIST_STATUS_FILTERS.filter((option) => option !== "open");

function StatusFilter({
  value,
  onChange,
}: Readonly<{
  value: WorklistStatusFilter | "";
  onChange: (next: WorklistStatusFilter | "") => void;
}>) {
  return (
    <div className={styles.filterGroup}>
      <span className={styles.filterLabel}>Status</span>
      <div className={styles.segment} role="group" aria-label="Status">
        <button
          type="button"
          className={styles.segmentButton}
          aria-pressed={value === ""}
          onClick={() => onChange("")}
        >
          {CASE_STATUS_LABELS.open} (default)
        </button>
        {STATUS_SEGMENTS.map((option) => (
          <button
            key={option}
            type="button"
            className={styles.segmentButton}
            aria-pressed={value === option}
            onClick={() => onChange(option)}
          >
            {CASE_STATUS_LABELS[option]}
          </button>
        ))}
      </div>
    </div>
  );
}

export function RecoveryScreen() {
  const permissions = usePermissions();
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [status, setStatus] = useState<WorklistStatusFilter | "">("");
  const [classification, setClassification] = useState<WorklistClass | "">("");
  const ownId = getOwnUserId();

  // The two DECLARED contract filters ONLY:
  // code-owned vocabulary values, bound server-side; the filters are
  // part of the query key so each combination is its own keyset walk
  // (never a locally filtered view of another one).
  const filters: WorklistFilters = { status, classification };
  const list = useKeysetList<WorklistRow>({
    queryKey: ["recovery", "worklist", filters],
    fetchPage: (cursor) => fetchWorklistPage(filters, cursor),
  });

  const mayOpen = can(permissions.data, "loan_book", "create");

  const columns: Column<WorklistRow>[] = [
    {
      key: "dpd",
      header: "Days past due",
      align: "right",
      render: (row) => <span className={styles.dpdCell}>{row.days_past_due}</span>,
    },
    {
      key: "classification",
      header: "Classification",
      render: (row) => loanClassPill(row.classification),
    },
    {
      key: "loan",
      header: "Loan",
      render: (row) => (
        <span className={styles.mono} title={row.loan_id}>
          {row.loan_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "member",
      header: "Member",
      render: (row) => (
        <span className={styles.mono} title={row.member_id}>
          {row.member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "assignee",
      header: "Assignee",
      render: (row) =>
        row.assignee_id !== null ? (
          <>
            {row.assignee_id === ownId ? (
              "you"
            ) : (
              <span className={styles.mono} title={row.assignee_id}>
                {row.assignee_id.slice(0, 8)}
              </span>
            )}{" "}
            {row.assignee_unassignable && (
              <Pill bg="brickSoft" color="brick">
                No longer assignable
              </Pill>
            )}
          </>
        ) : (
          <span className={styles.muted}>— (unassigned)</span>
        ),
    },
    {
      key: "opened",
      header: "Opened",
      render: (row) => <span className={styles.muted}>{fmtDateTime(row.opened_at)}</span>,
    },
    {
      key: "first_assigned",
      header: "First assigned",
      render: (row) => <span className={styles.muted}>{fmtDateTime(row.first_assigned_at)}</span>,
    },
  ];

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <StatusFilter value={status} onChange={setStatus} />
          {/* Labelled select (5 stored labels + All — over the ≤5
              segment budget): values are the code-owned LoanClass
              vocabulary verbatim; "" sends NO parameter at all. */}
          <FormField id="worklist-classification" label="Classification">
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={classification}
                onChange={(event) => setClassification(event.target.value as WorklistClass | "")}
              >
                <option value="">All classifications</option>
                {WORKLIST_CLASSES.map((option) => (
                  <option key={option} value={option}>
                    {LOAN_CLASS_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
        </div>
        {mayOpen && (
          <Button type="button" variant="primary" onClick={() => setDrawer({ mode: "open" })}>
            + Initiate recovery
          </Button>
        )}
      </div>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Recovery worklist</span>
          <span className={styles.registerNote}>
            Open cases, most overdue first — workflow facts only; no balances
            render here (those live behind the Loan book, for the entitled)
          </span>
        </div>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(row) => row.case_id}
          emptyMessage="No recovery cases in this view — no case matches the selected posture/classification (the default view lists OPEN cases, most overdue first)."
          onRowClick={(row) => setDrawer({ mode: "detail", caseId: row.case_id })}
        />
      </Card>

      {drawer !== null && drawer.mode === "open" && (
        <OpenCaseDrawer
          onOpened={(caseId) => setDrawer({ mode: "detail", caseId })}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "detail" && (
        <CaseDetailDrawer caseId={drawer.caseId} onClose={() => setDrawer(null)} />
      )}
    </div>
  );
}
