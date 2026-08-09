"use client";

/**
 * Dividends register (the lifecycle console): the declaration workflow's read surface plus entry points
 * for the lifecycle writes (declare → committee votes → void /
 * distribute).
 *
 * Security posture (exits/transactions precedent):
 * - Every rendered string (ids, server decimals, statuses) is treated
 *   as attacker-influenced data; it renders exclusively through React
 *   text interpolation — no parser sink exists in this module
 *   (gate-tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). "Declare dividend"
 *   mounts only with transactions:edit (the gate decision: the route itself is transactions:view via RequireModule); vote/void/
 *   distribute affordances mount in the drawers only with
 *   transactions:approve.
 * - Keyset pagination only (opaque cursors — scalability); the
 *   list contract declares NO filters — none are offered and nothing
 *   is filtered locally.
 * - MONEY (blocker (a)): every declaration figure (bases, dividend,
 *   rebate, payout) is an API decimal STRING rendered via fmtKes in
 *   its own labelled column, never summed or re-derived (no totals
 *   row exists; total_payout = dividend + rebate is migration 0020's
 *   DB CHECK, not a client computation).
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { fetchDeclarationsPage } from "../api";
import type { DeclarationRecord } from "../schemas";
import { declarationStatusPill } from "./pills";
import styles from "./Dividends.module.css";

// Drawer-level code splitting (speed): drawer chunks load
// on first open, not with the list route.
const DeclareDividendDrawer = dynamic(
  () => import("./DeclareDividendDrawer").then((m) => m.DeclareDividendDrawer),
  { ssr: false },
);
const DeclarationDetailDrawer = dynamic(
  () => import("./DeclarationDetailDrawer").then((m) => m.DeclarationDetailDrawer),
  { ssr: false },
);
const DistributeDialog = dynamic(
  () => import("./DistributeDialog").then((m) => m.DistributeDialog),
  { ssr: false },
);

type DrawerState =
  | null
  | { mode: "declare" }
  | { mode: "detail"; declarationId: string }
  | { mode: "distribute"; declarationId: string };

export function DividendsScreen() {
  const permissions = usePermissions();
  const [drawer, setDrawer] = useState<DrawerState>(null);

  const list = useKeysetList<DeclarationRecord>({
    queryKey: ["dividends", "list"],
    fetchPage: (cursor) => fetchDeclarationsPage(cursor),
  });

  // Declaring is back-office ledger governance (the gate
  // decision: transactions:edit — deliberately not transactions:create,
  // which would let counter tellers open tenant-wide payout workflows).
  const mayDeclare = can(permissions.data, "transactions", "edit");

  const columns: Column<DeclarationRecord>[] = [
    {
      key: "declared",
      header: "Declared",
      render: (declaration) => (
        <span className={styles.muted}>{fmtDateTime(declaration.created_at)}</span>
      ),
    },
    {
      key: "declared_by",
      header: "Declared by",
      // Declarer attribution: the SERVER's
      // bare staff UUID via the short-id convention — least
      // disclosure, no name/email is ever fetched for it. NULL is the
      // honest unattributed affordance — never invented.
      render: (declaration) =>
        declaration.requested_by !== null ? (
          <span className={styles.mono} title={declaration.requested_by}>
            {declaration.requested_by.slice(0, 8)}
          </span>
        ) : (
          <span className={styles.muted}>— (unattributed)</span>
        ),
    },
    {
      key: "fy",
      header: "Financial year",
      render: (declaration) => (
        // Calendar dates verbatim (date.isoformat()) — not instants;
        // fmtDateTime would invent a time of day for them.
        <span className={styles.muted}>
          {declaration.fy_start} → {declaration.fy_end}
        </span>
      ),
    },
    {
      key: "rates",
      header: "Rates (dividend / rebate % p.a.)",
      render: (declaration) => (
        <span className={styles.muted}>
          {declaration.dividend_rate_pct}% / {declaration.rebate_rate_pct}%
        </span>
      ),
    },
    {
      key: "eligible",
      header: "Eligible members",
      align: "right",
      render: (declaration) => (
        <span className={styles.amountCell}>{declaration.eligible_members}</span>
      ),
    },
    {
      key: "dividend",
      header: "Dividend",
      align: "right",
      render: (declaration) => (
        <span className={styles.amountCell}>{fmtKes(declaration.total_dividend)}</span>
      ),
    },
    {
      key: "rebate",
      header: "Rebate",
      align: "right",
      render: (declaration) => (
        <span className={styles.amountCell}>{fmtKes(declaration.total_rebate)}</span>
      ),
    },
    {
      key: "payout",
      header: "Total payout",
      align: "right",
      render: (declaration) => (
        <span className={styles.payoutCell}>{fmtKes(declaration.total_payout)}</span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (declaration) => declarationStatusPill(declaration.status),
    },
  ];

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={styles.toolbarNote}>
          Rates and the financial year come exclusively from tenant
          configuration (Settings ▸ Deposits &amp; shares); every figure below
          is a server-computed snapshot. Dormant members remain shareholders
          and are included in every declaration.
        </div>
        <div className={styles.toolbarActions}>
          {mayDeclare && (
            <Button type="button" variant="primary" onClick={() => setDrawer({ mode: "declare" })}>
              + Declare dividend
            </Button>
          )}
        </div>
      </div>

      <Card padded={false}>
        <div className={styles.registerHead}>
          <span>Dividend declarations</span>
          <span className={styles.registerNote}>
            Figures render verbatim — payout = dividend + rebate is the
            database&apos;s CHECK, not a client computation
          </span>
        </div>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(declaration) => declaration.id}
          emptyMessage="No dividend declarations yet."
          onRowClick={(declaration) => setDrawer({ mode: "detail", declarationId: declaration.id })}
        />
      </Card>

      {drawer !== null && drawer.mode === "declare" && (
        <DeclareDividendDrawer onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "detail" && (
        <DeclarationDetailDrawer
          declarationId={drawer.declarationId}
          onDistribute={(record) => setDrawer({ mode: "distribute", declarationId: record.id })}
          onClose={() => setDrawer(null)}
        />
      )}
      {drawer !== null && drawer.mode === "distribute" && (
        <DistributeDialog declarationId={drawer.declarationId} onClose={() => setDrawer(null)} />
      )}
    </div>
  );
}
