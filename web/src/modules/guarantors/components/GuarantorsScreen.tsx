"use client";

/**
 * Guarantors screen (module 5 — prototype `vGuar`, adapted to the P9// contract).
 *
 * Security posture (loans/applications precedent):
 * - Every rendered string (member names, ids) is attacker-influenced
 *   data; it renders exclusively through React text interpolation — no
 *   parser sink exists in this module (gate-tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (least disclosure). The pledge queue and the
 *   session panel mount only with applications:edit (the grant every
 *   guarantee write requires); a stripped role fetches NOTHING extra.
 * - MONEY (blocker (a)): active-guarantee counts, pledged totals and
 *   per-guarantor free capacity come from the SERVER's dashboard
 *   slice, rendered via fmtKes or verbatim with the server's as-of
 *   stamp. The prototype's client-side freeCap()/avg-capacity/
 *   utilisation math is deliberately NOT reproduced.
 * - The contract exposes NO guarantee list endpoint: the "session"
 *   panel shows only records THIS TAB witnessed from write responses
 *   (per-tab registry, the makerRegistry honesty precedent) — the
 *   MR records this limitation; nothing is fabricated.
 *
 * Layout: the screen's three stacked tables (capacity, the pledge
 * queue, the session registry) are separate TABS (design-system
 * TabList/TabPanel, reuse-first) — each mounts its own query/filter/
 * pagination only while its tab is active, so a hidden tab fetches
 * NOTHING. The aggregate STAT cards are not a table and stay outside
 * the tabs as a persistent overview strip.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";
import {
  Card,
  FilterControl,
  Stat,
  KeysetTable,
  type Column,
  type TabDef,
  TabList,
  TabPanel,
  useKeysetList,
  useKeysetPagination,
  ErrorBanner,
  grid,
} from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchDashboardSummary } from "@/modules/dashboard/api";
import type { DashboardSummary } from "@/modules/dashboard/schemas";
import { useProducts } from "@/modules/applications/useProducts";
import { fetchApplicationsPage } from "@/modules/applications/api";
import {
  STAGE_LABELS,
  type Application,
  type ApplicationStage,
} from "@/modules/applications/schemas";
import { coverPill } from "@/modules/applications/components/pills";
import { useWitnessedGuarantees, type WitnessedGuarantee } from "../sessionRegistry";
import { PLEDGEABLE_STAGES, type Guarantee, type PledgeableStage } from "../schemas";
import { guaranteeStatusPill } from "./pills";
import styles from "./Guarantors.module.css";

// Drawer-level code splitting (speed): drawer chunks load on
// first open, not with the list route.
const PledgeDrawer = dynamic(() => import("./PledgeDrawer").then((m) => m.PledgeDrawer), {
  ssr: false,
});
const GuaranteeActDialog = dynamic(
  () => import("./GuaranteeActDialog").then((m) => m.GuaranteeActDialog),
  { ssr: false },
);
const SubstituteDrawer = dynamic(
  () => import("./SubstituteDrawer").then((m) => m.SubstituteDrawer),
  { ssr: false },
);

function useGuarantorAggregates() {
  return useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: fetchDashboardSummary,
    // Composite class: server-computed snapshot aggregates.
    staleTime: STALE_TIME.composite,
  });
}

/** SERVER aggregate stat strip: counts + pledged total — every figure a
 * server fact with the server's as-of stamp. Persistent above the tabs
 * (not itself a table). */
function AggregateStats({ summary }: Readonly<{ summary: DashboardSummary }>) {
  const slice = summary.guarantors ?? null;
  if (slice === null) {
    return (
      <div className={styles.formNote}>
        The guarantor aggregates slice was not granted to this role — no figures are
        shown (nothing is computed client-side).
      </div>
    );
  }
  return (
    <div className={grid.cards4}>
      <Card>
        <Stat label="Active guarantees" value={String(slice.active_guarantees)} />
      </Card>
      <Card>
        <Stat label="Total pledged" value={fmtKes(slice.total_pledged)} />
      </Card>
      <Card>
        <Stat label="Distinct guarantors" value={String(slice.distinct_guarantors)} />
      </Card>
      <Card>
        <Stat label="Aggregates as of" value={fmtDateTime(summary.as_of)} />
        <div className={styles.statSub}>Server-computed snapshot</div>
      </Card>
    </div>
  );
}

/** Guarantor capacity table (tab 1) — the same bounded, server-capped
 * list the aggregate strip's query already carries; a dedicated
 * component so its content lives entirely inside its own tab panel. */
function GuarantorCapacityPanel({ summary }: Readonly<{ summary: DashboardSummary }>) {
  const slice = summary.guarantors ?? null;
  if (slice === null) {
    return (
      <div className={styles.formNote}>
        The guarantor aggregates slice was not granted to this role — no capacity
        table is shown.
      </div>
    );
  }
  return (
    <Card padded={false}>
      {slice.guarantors.length === 0 ? (
        <div className={styles.panelNote}>No live guarantors reported.</div>
      ) : (
        <div className={styles.miniTableWrap}>
          <table className={styles.miniTable}>
            <thead>
              <tr>
                <th scope="col">Guarantor</th>
                <th scope="col" className={styles.num}>
                  Pledged
                </th>
                <th scope="col" className={styles.num}>
                  Free capacity
                </th>
              </tr>
            </thead>
            <tbody>
              {slice.guarantors.map((row) => (
                <tr key={row.member_id}>
                  <td>
                    <div className={styles.cellStrong}>{row.name}</div>
                    <div className={styles.muted}>{row.member_no}</div>
                  </td>
                  <td className={styles.num}>{fmtKes(row.pledged_total)}</td>
                  <td className={styles.num}>{fmtKes(row.free_capacity)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/**
 * Applications open for pledging (the P9 pledgeable stages) — tab 2. Its
 * keyset hook mounts ONLY while this tab is active AND for operators
 * holding applications:edit; a stripped role or a hidden tab fetches
 * NOTHING (the useKeysetList primitive is consumed unmodified, reuse-first).
 * The stage filter is a SERVER query parameter.
 */
function PledgeQueue({
  columns,
  stage,
  onStageChange,
  onPledge,
}: Readonly<{
  columns: Column<Application>[];
  stage: PledgeableStage;
  onStageChange: (next: PledgeableStage) => void;
  onPledge: (applicationId: string) => void;
}>) {
  const pagination = useKeysetPagination();
  const filters = { stage: stage as ApplicationStage };
  const queue = useKeysetList<Application>({
    queryKey: ["applications", "list", filters, pagination.pageSize],
    fetchPage: (cursor) => fetchApplicationsPage(filters, cursor, pagination.pageSize),
  });
  return (
    <Card>
      <div className={styles.toolbar}>
        <div className={styles.filters}>
          <FilterControl
            id="pledge-stage-filter"
            label="Application stage"
            value={stage}
            onChange={(next) => {
              // allOption={false}: "" is never emitted — the pledgeable
              // vocabulary has no "all stages" state (the UI never offers
              // what the API forbids).
              if (next !== "") {
                pagination.setPageIndex(0);
                onStageChange(next);
              }
            }}
            options={PLEDGEABLE_STAGES.map((option) => ({
              value: option,
              label: STAGE_LABELS[option],
            }))}
            allOption={false}
          />
        </div>
      </div>
      
      <Card padded={false}>
        <KeysetTable
          columns={columns}
          query={queue}
          rowKey={(app) => app.id}
          emptyMessage="No applications in this stage."
          onRowClick={(app) => onPledge(app.id)}
          pagination={{
            pageIndex: pagination.pageIndex,
            pageSize: pagination.pageSize,
            onPageChange: pagination.setPageIndex,
            onPageSizeChange: pagination.setPageSize,
            rowLabel: "applications",
          }}
        />
      </Card>
    </Card>
  );
}

type ActOverlay =
  | { mode: "consent" | "release"; guarantee: Guarantee }
  | { mode: "substitute"; guarantee: Guarantee };

/** This session's guarantees (tab 3) — records witnessed by THIS tab
 * from write responses only (no list endpoint exists — the honesty
 * note stays verbatim from the prior single-page layout). */
function SessionGuaranteesPanel({
  witnessed,
  onAct,
}: Readonly<{
  witnessed: readonly WitnessedGuarantee[];
  onAct: (overlay: ActOverlay) => void;
}>) {
  return (
    <Card padded={false}>
      {witnessed.length === 0 ? (
        <div className={styles.panelNote}>
          No guarantees recorded in this session yet. Pledge one from the queue tab
          to carry it through consent and release here.
        </div>
      ) : (
        <div className={styles.miniTableWrap}>
          <table className={styles.miniTable}>
            <caption className={styles.formNote}>
              Session-witnessed guarantee records, newest activity first.
            </caption>
            <thead>
              <tr>
                <th scope="col">Record</th>
                <th scope="col">Guarantor</th>
                <th scope="col">Borrower</th>
                <th scope="col" className={styles.num}>
                  Amount
                </th>
                <th scope="col">Status</th>
                <th scope="col" className={styles.num}>
                  Actions
                </th>
              </tr>
            </thead>
            <tbody>
              {witnessed.map(({ record, conflicted }) => {
                const idPrefix = record.id.slice(0, 8);
                const mayConsent = !conflicted && record.status === "pledged";
                const mayRelease =
                  !conflicted &&
                  (record.status === "pledged" ||
                    (record.status === "active" && record.loan_id === null));
                const maySubstitute =
                  !conflicted && record.status === "active" && record.loan_id !== null;
                return (
                  <tr key={record.id}>
                    <td>
                      <span className={styles.mono} title={record.id}>
                        {idPrefix}
                      </span>
                    </td>
                    <td>
                      <span className={styles.mono} title={record.guarantor_member_id}>
                        {record.guarantor_member_id.slice(0, 8)}
                      </span>
                    </td>
                    <td>
                      <span className={styles.mono} title={record.borrower_member_id}>
                        {record.borrower_member_id.slice(0, 8)}
                      </span>
                    </td>
                    <td className={styles.num}>
                      <span className={styles.cellStrong}>{fmtKes(record.amount)}</span>
                    </td>
                    <td>
                      {guaranteeStatusPill(record.status)}
                      {conflicted && (
                        <div className={styles.formNote}>
                          Changed outside this tab — actions withdrawn.
                        </div>
                      )}
                    </td>
                    <td>
                      <div className={styles.rowActions}>
                        {mayConsent && (
                          <button
                            type="button"
                            className={styles.segmentButton}
                            onClick={() => onAct({ mode: "consent", guarantee: record })}
                          >
                            Consent…
                          </button>
                        )}
                        {mayRelease && (
                          <button
                            type="button"
                            className={styles.segmentButton}
                            onClick={() => onAct({ mode: "release", guarantee: record })}
                          >
                            Release…
                          </button>
                        )}
                        {maySubstitute && (
                          <button
                            type="button"
                            className={styles.segmentButton}
                            onClick={() => onAct({ mode: "substitute", guarantee: record })}
                          >
                            Substitute…
                          </button>
                        )}
                        {!mayConsent && !mayRelease && !maySubstitute && (
                          <span className={styles.muted}>—</span>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

type Overlay = null | { mode: "pledge"; applicationId: string } | ActOverlay;

type GuarantorTabId = "capacity" | "pledge" | "session";

export function GuarantorsScreen() {
  const permissions = usePermissions();
  const products = useProducts();
  const summary = useGuarantorAggregates();
  const [stage, setStage] = useState<PledgeableStage>("submitted");
  const [overlay, setOverlay] = useState<Overlay>(null);
  const [activeTab, setActiveTab] = useState<GuarantorTabId>("capacity");
  const witnessed = useWitnessedGuarantees();

  // Every guarantee write is gated on applications:edit server-side
  // (guarantor self-release path is member-facing).
  const mayPledge = can(permissions.data, "applications", "edit");

  const productName = (productId: string): string => {
    const product = products.data?.find((p) => p.id === productId);
    // Fallback: the opaque id as inert text (reference data unavailable).
    return product?.name ?? productId.slice(0, 8);
  };

  const queueColumns: Column<Application>[] = [
    {
      key: "member",
      header: "Borrower",
      render: (app) =>
        // Identifier doctrine: number — name, resolved server-side on
        // the row; the uuid stays machine identity (title).
        app.member_no !== null ? (
          <div title={app.member_id}>
            <div className={styles.cellStrong}>{app.member_name}</div>
            <div className={styles.cellSub}>{app.member_no}</div>
          </div>
        ) : (
          <span className={styles.mono} title={app.member_id}>
            {app.member_id.slice(0, 8)}
          </span>
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
      key: "amount",
      header: "Applied amount",
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
      render: (app) => <span className={styles.muted}>{STAGE_LABELS[app.stage]}</span>,
    },
    {
      key: "action",
      header: "",
      align: "right",
      render: () => <span className={styles.cellStrong}>Pledge ›</span>,
    },
  ];

  const tabs: TabDef[] = mayPledge
    ? [
      { id: "capacity", label: "Guarantor capacity" },
      { id: "pledge", label: "Open for pledging" },
      { id: "session", label: "This session's guarantees" },
    ]
    : [{ id: "capacity", label: "Guarantor capacity" }];

  return (
    <div>
      <div className={styles.section}>
        {summary.isPending ? (
          <div className={styles.muted}>Loading guarantor aggregates…</div>
        ) : summary.isError ? (
          <ErrorBanner error={summary.error} />
        ) : (
          <AggregateStats summary={summary.data} />
        )}
      </div>

      <div className={styles.section}>
        <TabList
          idPrefix="guarantors"
          tabs={tabs}
          activeId={activeTab}
          onChange={(next) => setActiveTab(next as GuarantorTabId)}
          ariaLabel="Guarantor views"
        />
      </div>

      <TabPanel idPrefix="guarantors" activeTabId={activeTab}>
        {activeTab === "capacity" &&
          (summary.isPending ? (
            <div className={styles.muted}>Loading guarantor capacity…</div>
          ) : summary.isError ? (
            <ErrorBanner error={summary.error} />
          ) : (
            <GuarantorCapacityPanel summary={summary.data} />
          ))}

        {activeTab === "pledge" && mayPledge && (
          <PledgeQueue
            columns={queueColumns}
            stage={stage}
            onStageChange={setStage}
            onPledge={(applicationId) => setOverlay({ mode: "pledge", applicationId })}
          />
        )}

        {activeTab === "session" && mayPledge && (
          <SessionGuaranteesPanel witnessed={witnessed} onAct={setOverlay} />
        )}
      </TabPanel>

      {overlay !== null && overlay.mode === "pledge" && (
        <PledgeDrawer
          applicationId={overlay.applicationId}
          products={products.data ?? []}
          onClose={() => setOverlay(null)}
        />
      )}
      {overlay !== null && (overlay.mode === "consent" || overlay.mode === "release") && (
        <GuaranteeActDialog
          guarantee={overlay.guarantee}
          act={overlay.mode}
          onClose={() => setOverlay(null)}
        />
      )}
      {overlay !== null && overlay.mode === "substitute" && (
        <SubstituteDrawer guarantee={overlay.guarantee} onClose={() => setOverlay(null)} />
      )}
    </div>
  );
}
