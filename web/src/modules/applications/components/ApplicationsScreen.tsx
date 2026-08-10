"use client";

/**
 * Loan-applications register (module 3 — prototype `vApps`).
 *
 * Security posture (users/members precedent):
 * - Every rendered string (purpose, product names, ids) is
 *   attacker-influenced data; it renders exclusively through React text
 *   interpolation — no parser sink exists in this module (gate-tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure).
 * - Keyset pagination only (opaque cursors — scalability).
 * - MONEY (blocker (a)): the amount and cover% are API decimal STRINGS
 *   rendered via fmtKes or verbatim — nothing monetary is computed here. Stage
 *   pipeline COUNTS are deliberately absent: the server-computed counts
 *   live on the dashboard composite; reconstructing them client-side
 *   from keyset pages would be wrong and is not attempted.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Banner, Button, Card, FilterControl } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { useKeysetPagination } from "@/modules/table/KeysetPaginator";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtAmount } from "@/lib/format";
import { fetchApplicationsPage, type ApplicationListFilters } from "../api";
import { useProducts } from "../useProducts";
import {
  APPLICATION_STAGES,
  STAGE_LABELS,
  type Application,
  type ApplicationStage,
  type Product,
} from "../schemas";
import {
  useDashboardSummary,
} from "@/modules/dashboard/components/DashboardScreen";
import { coverPill, stagePill } from "./pills";
import styles from "./Applications.module.css";

// Drawer-level code splitting (speed): drawer chunks load on
// first open, not with the list route.
const ApplicationCreateDrawer = dynamic(
  () => import("./ApplicationCreateDrawer").then((m) => m.ApplicationCreateDrawer),
  { ssr: false },
);
const ApplicationDetailDrawer = dynamic(
  () => import("./ApplicationDetailDrawer").then((m) => m.ApplicationDetailDrawer),
  { ssr: false },
);

type DrawerState = null | { mode: "create" } | { mode: "detail"; applicationId: string };

export function ApplicationsScreen() {
  const permissions = usePermissions();
  const products = useProducts();
  const dashboard = useDashboardSummary();
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [notice, setNotice] = useState<string>("");
  const pagination = useKeysetPagination();
  const [stage, setStageRaw] = useState<ApplicationStage | "">("");

  // Reset to page 0 whenever the stage filter changes.
  function setStage(next: ApplicationStage | "") {
    setStageRaw(next);
    pagination.setPageIndex(0);
  }

  // Build a stage → count lookup from the dashboard pipeline slice.
  // Counts are advisory badges only — null when the slice is loading or absent.
  const pipelineLookup = new Map(
    (dashboard.data?.pipeline ?? []).map((row) => [row.stage, row.count]),
  );
  const stageCounts = APPLICATION_STAGES.map((s) => ({
    value: s,
    label: STAGE_LABELS[s],
    count: pipelineLookup.get(s) ?? null,
  }));
  // Total across all stages — null until the pipeline slice loads.
  const allStagesCount =
    dashboard.data?.pipeline != null
      ? dashboard.data.pipeline.reduce((sum, row) => sum + row.count, 0)
      : null;

  // pageSize drives both the API limit and the display slice.
  // Changing stage resets to page 0 so we start fresh.
  const filters: ApplicationListFilters = { stage };
  const list = useKeysetList<Application>({
    queryKey: ["applications", "list", filters, pagination.pageSize],
    fetchPage: (cursor) => fetchApplicationsPage(filters, cursor, pagination.pageSize),
  });

  const mayCreate = can(permissions.data, "applications", "create");

  const productName = (productId: string): string => {
    const product = products.data?.find((p: Product) => p.id === productId);
    // Fallback: the opaque id as inert text (reference data unavailable).
    return product?.name ?? productId.slice(0, 8);
  };

  const columns: Column<Application>[] = [
    {
      key: "member",
      header: "Member",
      render: (app) => (
        <div>
          {/* Identifier doctrine: number — name, resolved server-side
              on the row; the uuid stays machine identity (title). */}
          {app.member_no !== null ? (
            <span title={app.member_id}>
              {app.member_no} — {app.member_name}
            </span>
          ) : (
            <span className={styles.mono} title={app.member_id}>
              {app.member_id.slice(0, 8)}
            </span>
          )}
        </div>
      ),
    },
    {
      key: "product",
      header: "Product",
      render: (app) => (
        <span className={styles.muted}>{app.product_name ?? productName(app.product_id)}</span>
      ),
    },
    {
      key: "purpose",
      header: "Purpose",
      render: (app) => <span className={styles.muted}>{app.purpose ?? "—"}</span>,
    },
    {
      key: "amount",
      header: "Amount (KES)",
      align: "right",
      render: (app) => <span className={styles.cellStrong}>{fmtAmount(app.amount)}</span>,
    },
    {
      key: "cover",
      header: "Cover",
      align: "right",
      render: (app) => coverPill(app.cover_pct),
    },
    {
      key: "stage",
      header: "Stage",
      align: "right",
      render: (app) => stagePill(app.stage),
    },
  ];

  return (
    <Card>
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <FilterControl
            id="app-stage-filter"
            label="Stage"
            value={stage}
            onChange={setStage}
            options={stageCounts}
            allLabel="All stages"
            allCount={allStagesCount}
          />
        </div>
        {mayCreate && (
          <Button variant="primary" onClick={() => setDrawer({ mode: "create" })}>
            + New application
          </Button>
        )}
      </div>
      {notice !== "" && <Banner variant="ok">{notice}</Banner>}
      <Card padded={false}>
        <KeysetTable
          columns={columns}
          query={list}
          rowKey={(app) => app.id}
          emptyMessage="No applications match this filter."
          onRowClick={(app) => setDrawer({ mode: "detail", applicationId: app.id })}
          pagination={{
            pageIndex: pagination.pageIndex,
            pageSize: pagination.pageSize,
            onPageChange: pagination.setPageIndex,
            onPageSizeChange: pagination.setPageSize,
            rowLabel: "applications",
          }}
        />
      </Card>
      {drawer !== null && drawer.mode === "create" && (
        <ApplicationCreateDrawer
          products={products.data ?? []}
          onClose={() => setDrawer(null)}
          onCreated={(app) => {
            setNotice("Application submitted for appraisal review.");
            setDrawer({ mode: "detail", applicationId: app.id });
          }}
        />
      )}
      {drawer !== null && drawer.mode === "detail" && (
        <ApplicationDetailDrawer
          applicationId={drawer.applicationId}
          products={products.data ?? []}
          onClose={() => setDrawer(null)}
        />
      )}
    </Card>
  );
}
