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
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchDashboardSummary } from "@/modules/dashboard/api";
import { useProducts } from "@/modules/applications/useProducts";
import { fetchApplicationsPage } from "@/modules/applications/api";
import {
  STAGE_LABELS,
  type Application,
  type ApplicationStage,
} from "@/modules/applications/schemas";
import { coverPill } from "@/modules/applications/components/pills";
import { useWitnessedGuarantees } from "../sessionRegistry";
import { PLEDGEABLE_STAGES, type Guarantee, type PledgeableStage } from "../schemas";
import { guaranteeStatusPill } from "./pills";
import grid from "@/modules/layout/grid.module.css";
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

/**
 * SERVER aggregates (guarantors slice): counts, pledged total and
 * the per-guarantor capacity panel — every figure a server fact with
 * the server's as-of stamp. When the slice is not granted the endpoint
 * omits it and an honest note renders instead (nothing is fabricated).
 */
function AggregatesStrip() {
  const summary = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: fetchDashboardSummary,
    // Composite class: server-computed snapshot aggregates.
    staleTime: STALE_TIME.composite,
  });

  if (summary.isPending) {
    return <div className={styles.muted}>Loading guarantor aggregates…</div>;
  }
  if (summary.isError) {
    return <ErrorBanner error={summary.error} />;
  }
  const slice = summary.data.guarantors ?? null;
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
        <Stat label="Aggregates as of" value={fmtDateTime(summary.data.as_of)} />
        <div className={styles.statSub}>Server-computed snapshot</div>
      </Card>
      <Card className={grid.wide}>
        <div className={styles.resultTitle}>Guarantor capacity (server slice)</div>
        {slice.guarantors.length === 0 ? (
          <div className={styles.muted}>No live guarantors reported.</div>
        ) : (
          <div className={styles.miniTableWrap}>
            <table className={styles.miniTable}>
              <caption className={styles.formNote}>
                Members with live pledges, largest exposure first — pledged and free
                figures are the server&apos;s.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Guarantor</th>
                  <th scope="col">Member no</th>
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
                    <td className={styles.cellStrong}>{row.name}</td>
                    <td className={styles.muted}>{row.member_no}</td>
                    <td className={styles.num}>{fmtKes(row.pledged_total)}</td>
                    <td className={styles.num}>{fmtKes(row.free_capacity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function StageFilter({
  value,
  onChange,
}: Readonly<{
  value: PledgeableStage;
  onChange: (next: PledgeableStage) => void;
}>) {
  return (
    <div className={styles.filterGroup}>
      <span className={styles.filterLabel}>Application stage</span>
      <div className={styles.segment} role="group" aria-label="Application stage">
        {PLEDGEABLE_STAGES.map((option) => (
          <button
            key={option}
            type="button"
            className={styles.segmentButton}
            aria-pressed={value === option}
            onClick={() => onChange(option)}
          >
            {STAGE_LABELS[option]}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * Applications open for pledging (the P9 pledgeable stages) — a
 * separate component so its keyset hook mounts ONLY for operators
 * holding applications:edit; a stripped role fetches NOTHING (the useKeysetList primitive is consumed unmodified, reuse-first). The stage
 * filter is a SERVER query parameter.
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
  const filters = { stage: stage as ApplicationStage };
  const queue = useKeysetList<Application>({
    queryKey: ["applications", "list", filters],
    fetchPage: (cursor) => fetchApplicationsPage(filters, cursor),
  });
  return (
    <Card padded={false}>
      <div className={styles.queueHead}>
        <span>Open for pledging</span>
        <span className={styles.queueNote}>
          Applications in the pledgeable stages — the server refuses pledges elsewhere
        </span>
      </div>
      <div className={styles.queueToolbar}>
        <StageFilter value={stage} onChange={onStageChange} />
      </div>
      <KeysetTable
        columns={columns}
        query={queue}
        rowKey={(app) => app.id}
        emptyMessage="No applications in this stage."
        onRowClick={(app) => onPledge(app.id)}
      />
    </Card>
  );
}

type Overlay =
  | null
  | { mode: "pledge"; applicationId: string }
  | { mode: "consent" | "release"; guarantee: Guarantee }
  | { mode: "substitute"; guarantee: Guarantee };

export function GuarantorsScreen() {
  const permissions = usePermissions();
  const products = useProducts();
  const [stage, setStage] = useState<PledgeableStage>("submitted");
  const [overlay, setOverlay] = useState<Overlay>(null);
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
      render: (app) => (
        // The P9 list carries member_id only (no joined name) — the
        // pledge drawer resolves the member record (precedent).
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

  return (
    <div>
      <div className={styles.section}>
        <AggregatesStrip />
      </div>

      {mayPledge && (
        <div className={styles.section}>
          <PledgeQueue
            columns={queueColumns}
            stage={stage}
            onStageChange={setStage}
            onPledge={(applicationId) => setOverlay({ mode: "pledge", applicationId })}
          />
        </div>
      )}

      {mayPledge && (
        <Card padded={false}>
          <div className={styles.queueHead}>
            <span>This session&apos;s guarantees</span>
            <span className={styles.queueNote}>
              Records witnessed by this tab from server responses — the contract exposes
              no guarantee list to read others
            </span>
          </div>
          {witnessed.length === 0 ? (
            <div className={styles.panelNote}>
              No guarantees recorded in this session yet. Pledge one from the queue above
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
                                onClick={() =>
                                  setOverlay({ mode: "consent", guarantee: record })
                                }
                              >
                                Consent…
                              </button>
                            )}
                            {mayRelease && (
                              <button
                                type="button"
                                className={styles.segmentButton}
                                onClick={() =>
                                  setOverlay({ mode: "release", guarantee: record })
                                }
                              >
                                Release…
                              </button>
                            )}
                            {maySubstitute && (
                              <button
                                type="button"
                                className={styles.segmentButton}
                                onClick={() =>
                                  setOverlay({ mode: "substitute", guarantee: record })
                                }
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
      )}

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
