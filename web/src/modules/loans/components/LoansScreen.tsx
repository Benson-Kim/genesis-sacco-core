"use client";

/**
 * Loan-book register (module 4 — prototype `vBook`).
 *
 * Security posture (applications/users precedent):
 * - Every rendered string (product names, ids, txn refs) is
 *   attacker-influenced data; it renders exclusively through React text
 *   interpolation — no parser sink exists in this module (gate-tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). The disbursement queue
 *   mounts only with loan_book:create AND applications:view (its data
 *   is the approved-applications list, consumed READ-ONLY from the
 *   applications module).
 * - Keyset pagination only (opaque cursors — scalability); status and
 *   classification filters are SERVER query parameters.
 * - MONEY (blocker (a)): balances, penalties and portfolio aggregates
 *   are API decimal STRINGS rendered via fmtKes or verbatim. The
 *   prototype's client-side portfolio reductions and classify(days)
 *   are deliberately NOT reproduced — the summary strip renders
 *   GET /portfolio/summary, the same server model that feeds the
 *   dashboard slice.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";
import { Card, Stat } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { useProducts } from "@/modules/applications/useProducts";
import { fetchApplicationsPage } from "@/modules/applications/api";
import type { Application } from "@/modules/applications/schemas";
import { coverPill } from "@/modules/applications/components/pills";
import { fetchLoansPage, fetchPortfolioSummary, type LoanListFilters } from "../api";
import {
  LOAN_CLASSES,
  LOAN_CLASS_LABELS,
  LOAN_STATUSES,
  LOAN_STATUS_LABELS,
  type Loan,
  type LoanClass,
  type LoanStatus,
} from "../schemas";
import { loanClassPill, loanStatusPill } from "./pills";
import grid from "@/modules/layout/grid.module.css";
import styles from "./Loans.module.css";

// Drawer-level code splitting (speed): drawer chunks load on
// first open, not with the list route.
const LoanDetailDrawer = dynamic(
  () => import("./LoanDetailDrawer").then((m) => m.LoanDetailDrawer),
  { ssr: false },
);
const DisburseDialog = dynamic(
  () => import("./DisburseDialog").then((m) => m.DisburseDialog),
  { ssr: false },
);

/** Approved applications awaiting funding — SERVER stage filter. */
const QUEUE_FILTERS = { stage: "approved" } as const;

function PortfolioSummaryStrip() {
  const summary = useQuery({
    queryKey: ["portfolio", "summary"],
    queryFn: fetchPortfolioSummary,
    // Composite class: server-computed snapshot aggregates.
    staleTime: STALE_TIME.composite,
  });

  if (summary.isPending) {
    return <div className={styles.muted}>Loading portfolio summary…</div>;
  }
  if (summary.isError) {
    return <ErrorBanner error={summary.error} />;
  }
  const data = summary.data;
  return (
    <div className={grid.cards4}>
      <Card>
        <Stat label="Outstanding balance" value={fmtKes(data.outstanding_balance)} />
        <div className={styles.statSub}>{data.active_loans} active loans</div>
      </Card>
      <Card>
        <Stat label="Non-performing" value={fmtKes(data.npl_balance)} />
        <div className={styles.statSub}>{data.npl_ratio_pct}% of book</div>
      </Card>
      <Card>
        <Stat label={"PAR > 30"} value={fmtKes(data.par30_balance)} />
        <div className={styles.statSub}>{data.par30_ratio_pct}% of book</div>
      </Card>
      <Card>
        <Stat label="Provisions" value={fmtKes(data.provisions)} />
        <div className={styles.statSub}>Penalties due {fmtKes(data.penalties_due)}</div>
      </Card>
      {data.by_classification.length > 0 && (
        <Card className={grid.wide}>
          <div className={styles.resultTitle}>By classification</div>
          <div className={styles.sliceList}>
            {data.by_classification.map((slice) => (
              <span key={slice.classification} className={styles.sliceItem}>
                {loanClassPill(slice.classification)}
                {slice.count} · {fmtKes(slice.balance)} · provisions {fmtKes(slice.provisions)}
              </span>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

function SegmentFilter<T extends string>({
  label,
  value,
  options,
  optionLabels,
  onChange,
}: Readonly<{
  label: string;
  value: T | "";
  options: readonly T[];
  optionLabels: Record<T, string>;
  onChange: (next: T | "") => void;
}>) {
  return (
    <div className={styles.filterGroup}>
      <span className={styles.filterLabel}>{label}</span>
      <div className={styles.segment} role="group" aria-label={label}>
        <button
          type="button"
          className={styles.segmentButton}
          aria-pressed={value === ""}
          onClick={() => onChange("")}
        >
          All
        </button>
        {options.map((option) => (
          <button
            key={option}
            type="button"
            className={styles.segmentButton}
            aria-pressed={value === option}
            onClick={() => onChange(option)}
          >
            {optionLabels[option]}
          </button>
        ))}
      </div>
    </div>
  );
}

type DrawerState =
  | null
  | { mode: "loan"; loanId: string }
  | { mode: "disburse"; applicationId: string };

/**
 * Approved-applications queue. A separate component so its keyset hook
 * mounts ONLY for operators holding loan_book:create AND
 * applications:view — a stripped role fetches NOTHING (the useKeysetList primitive is consumed unmodified, reuse-first).
 */
function DisburseQueue({
  columns,
  onDisburse,
}: Readonly<{
  columns: Column<Application>[];
  onDisburse: (applicationId: string) => void;
}>) {
  const queue = useKeysetList<Application>({
    queryKey: ["applications", "list", QUEUE_FILTERS],
    fetchPage: (cursor) => fetchApplicationsPage(QUEUE_FILTERS, cursor),
  });
  return (
    <Card padded={false}>
      <div className={styles.queueHead}>
        <span>Disbursement queue</span>
        <span className={styles.queueNote}>
          Committee-approved applications awaiting funding
        </span>
      </div>
      <KeysetTable
        columns={columns}
        query={queue}
        rowKey={(app) => app.id}
        emptyMessage="No approved applications awaiting disbursement."
        onRowClick={(app) => onDisburse(app.id)}
      />
    </Card>
  );
}

export function LoansScreen() {
  const permissions = usePermissions();
  const products = useProducts();
  const [status, setStatus] = useState<LoanStatus | "">("");
  const [classification, setClassification] = useState<LoanClass | "">("");
  const [drawer, setDrawer] = useState<DrawerState>(null);

  const filters: LoanListFilters = { status, classification };
  const list = useKeysetList<Loan>({
    queryKey: ["loans", "list", filters],
    fetchPage: (cursor) => fetchLoansPage(filters, cursor),
  });

  // The disbursement queue consumes the approved-applications list
  // READ-ONLY from the applications module; the POST itself is guarded
  // by loan_book:create server-side (P4 matrix).
  const mayDisburse =
    can(permissions.data, "loan_book", "create") &&
    can(permissions.data, "applications", "view");

  const productName = (productId: string): string => {
    const product = products.data?.find((p) => p.id === productId);
    // Fallback: the opaque id as inert text (reference data unavailable).
    return product?.name ?? productId.slice(0, 8);
  };

  const loanColumns: Column<Loan>[] = [
    {
      key: "member",
      header: "Member",
      render: (loan) => (
        // The list carries member_id only (no joined name) — the
        // detail drawer resolves the member record (precedent).
        <span className={styles.mono} title={loan.member_id}>
          {loan.member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "product",
      header: "Product",
      render: (loan) => <span className={styles.muted}>{productName(loan.product_id)}</span>,
    },
    {
      key: "balance",
      header: "Balance",
      align: "right",
      render: (loan) => <span className={styles.cellStrong}>{fmtKes(loan.balance)}</span>,
    },
    {
      key: "penalty",
      header: "Penalty due",
      align: "right",
      render: (loan) => <span className={styles.muted}>{fmtKes(loan.penalty_due)}</span>,
    },
    {
      key: "arrears",
      header: "Arrears",
      align: "right",
      render: (loan) => <span className={styles.muted}>{loan.days_past_due}d</span>,
    },
    {
      key: "class",
      header: "Class",
      align: "right",
      render: (loan) => loanClassPill(loan.classification),
    },
    {
      key: "status",
      header: "Status",
      align: "right",
      render: (loan) => loanStatusPill(loan.status),
    },
  ];

  const queueColumns: Column<Application>[] = [
    {
      key: "member",
      header: "Member",
      render: (app) => (
        <span className={styles.mono} title={app.member_id}>
          {app.member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "product",
      header: "Product",
      render: (app) => <span className={styles.muted}>{productName(app.product_id)}</span>,
    },
    {
      key: "amount",
      header: "Approved amount",
      align: "right",
      render: (app) => <span className={styles.cellStrong}>{fmtKes(app.amount)}</span>,
    },
    {
      key: "term",
      header: "Term",
      align: "right",
      render: (app) => <span className={styles.muted}>{app.term_months} mo</span>,
    },
    {
      key: "cover",
      header: "Cover",
      align: "right",
      render: (app) => coverPill(app.cover_pct),
    },
    {
      key: "action",
      header: "",
      align: "right",
      render: () => <span className={styles.cellStrong}>Disburse ›</span>,
    },
  ];

  return (
    <div>
      <div className={styles.section}>
        <PortfolioSummaryStrip />
      </div>

      {mayDisburse && (
        <div className={styles.section}>
          <DisburseQueue
            columns={queueColumns}
            onDisburse={(applicationId) => setDrawer({ mode: "disburse", applicationId })}
          />
        </div>
      )}

      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <SegmentFilter
            label="Status"
            value={status}
            options={LOAN_STATUSES}
            optionLabels={LOAN_STATUS_LABELS}
            onChange={setStatus}
          />
          <SegmentFilter
            label="Classification"
            value={classification}
            options={LOAN_CLASSES}
            optionLabels={LOAN_CLASS_LABELS}
            onChange={setClassification}
          />
        </div>
      </div>
      <Card padded={false}>
        <KeysetTable
          columns={loanColumns}
          query={list}
          rowKey={(loan) => loan.id}
          emptyMessage="No loans match this filter."
          onRowClick={(loan) => setDrawer({ mode: "loan", loanId: loan.id })}
        />
      </Card>

      {drawer !== null && drawer.mode === "loan" && (
        <LoanDetailDrawer
          loanId={drawer.loanId}
          products={products.data ?? []}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "disburse" && (
        <DisburseDialog
          applicationId={drawer.applicationId}
          products={products.data ?? []}
          onClose={() => setDrawer(null)}
        />
      )}
    </div>
  );
}
