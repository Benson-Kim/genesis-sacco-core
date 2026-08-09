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
import { Banner, Button, Card } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtKes } from "@/lib/format";
import { fetchApplicationsPage, type ApplicationListFilters } from "../api";
import { useProducts } from "../useProducts";
import {
  APPLICATION_STAGES,
  STAGE_LABELS,
  type Application,
  type ApplicationStage,
  type Product,
} from "../schemas";
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

function StageFilter({
  value,
  onChange,
}: Readonly<{
  value: ApplicationStage | "";
  onChange: (next: ApplicationStage | "") => void;
}>) {
  return (
    <div className={styles.filterGroup}>
      <span className={styles.filterLabel}>Stage</span>
      <div className={styles.segment} role="group" aria-label="Stage">
        <button
          type="button"
          className={styles.segmentButton}
          aria-pressed={value === ""}
          onClick={() => onChange("")}
        >
          All
        </button>
        {APPLICATION_STAGES.map((stage) => (
          <button
            key={stage}
            type="button"
            className={styles.segmentButton}
            aria-pressed={value === stage}
            onClick={() => onChange(stage)}
          >
            {STAGE_LABELS[stage]}
          </button>
        ))}
      </div>
    </div>
  );
}

type DrawerState = null | { mode: "create" } | { mode: "detail"; applicationId: string };

export function ApplicationsScreen() {
  const permissions = usePermissions();
  const products = useProducts();
  const [stage, setStage] = useState<ApplicationStage | "">("");
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [notice, setNotice] = useState<string>("");

  const filters: ApplicationListFilters = { stage };
  const list = useKeysetList<Application>({
    queryKey: ["applications", "list", filters],
    fetchPage: (cursor) => fetchApplicationsPage(filters, cursor),
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
          {/* The P9 list carries member_id only (no joined name) — the
              detail drawer resolves the member record. Follow-up scope:
              a display name in the contract. */}
          <span className={styles.mono} title={app.member_id}>
            {app.member_id.slice(0, 8)}
          </span>
        </div>
      ),
    },
    {
      key: "product",
      header: "Product",
      render: (app) => <span className={styles.muted}>{productName(app.product_id)}</span>,
    },
    {
      key: "purpose",
      header: "Purpose",
      render: (app) => <span className={styles.muted}>{app.purpose ?? "—"}</span>,
    },
    {
      key: "amount",
      header: "Amount",
      align: "right",
      render: (app) => <span className={styles.cellStrong}>{fmtKes(app.amount)}</span>,
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
    <div>
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <StageFilter value={stage} onChange={setStage} />
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
    </div>
  );
}
