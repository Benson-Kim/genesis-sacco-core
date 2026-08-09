"use client";

/**
 * Reports & exports screen (module 8 — prototype `vRep`, adapted to the contract).
 *
 * WHAT THE CONTRACT EXPOSES (and therefore what exists here): the
 * export workflow ONLY — request an export by `ReportName`, check its
 * requester-only status, drain the tenant queue, download the rendered
 * CSV/PDF via expiring capability tokens. There is NO in-browser
 * report-COMPUTING endpoint: the prototype's on-screen tables are NOT
 * reproduced from client-side queries (that would require exactly the
 * client-computed figures blocker (a) forbids). The on-screen view
 * (ViewExportDrawer) instead renders the SERVER's
 * OWN rendered CSV artifact verbatim through the existing download
 * route — zero contract change; downloads stay canonical. There is
 * still NO export list endpoint (the session panel below shows only
 * exports THIS TAB witnessed — the per-tab registry precedent,
 * recorded honestly).
 *
 * Security posture (transactions/guarantors precedent):
 * - Every rendered string (report names, statuses, filters, column
 *   lists, ids) is attacker-influenced data; it renders exclusively
 *   through React text interpolation — no parser sink exists in this
 *   module (gate-tested).
 * - The route mounts under deny-by-default RequireModule("reports");
 *   the API enforces reports:view on every call regardless (least disclosure).
 * - Downloads ride the GENERATED client + the shared downloadExport
 *   helper: bearer/tenant as HEADERS; the capability token is the
 *   contract's own path parameter; failures are least-disclosure.
 * - Nothing on this screen computes ANY figure: row counts, caps,
 *   truncation flags and queue counts are SERVER metadata verbatim.
 */
import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card, Pill } from "@genesis/design-system";
import { useMutation } from "@tanstack/react-query";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { fmtDateTime } from "@/lib/format";
import grid from "@/modules/layout/grid.module.css";
import { downloadArtifact, fetchExport } from "../api";
import { recordWitnessedExport, useWitnessedExports } from "../exportRegistry";
import {
  DATE_RE,
  REPORT_FILTERS,
  REPORT_LABELS,
  REPORT_NAMES,
  exportFilenameStem,
  reportLabel,
  type Artifact,
  type ExportOut,
  type ReportName,
} from "../schemas";
import styles from "./Reports.module.css";

// Drawer-level code splitting (speed): drawer chunks load
// on first open, not with the route.
const RequestExportDrawer = dynamic(
  () => import("./RequestExportDrawer").then((m) => m.RequestExportDrawer),
  { ssr: false },
);
const RunQueueDialog = dynamic(() => import("./RunQueueDialog").then((m) => m.RunQueueDialog), {
  ssr: false,
});
const ViewExportDrawer = dynamic(
  () => import("./ViewExportDrawer").then((m) => m.ViewExportDrawer),
  { ssr: false },
);

type DrawerState =
  | null
  | { mode: "request"; report: ReportName }
  | { mode: "queue" }
  | { mode: "view"; record: ExportOut; artifact: Artifact };

/** Operator-facing scope line per catalogue card, derived from the
 * report's declared filter surface (never more, never less). */
function scopeLine(report: ReportName): string {
  const surface = REPORT_FILTERS[report];
  if (surface.allowed.length === 0) {
    return "Whole tenant — no filters.";
  }
  const parts: string[] = [];
  for (const key of surface.allowed) {
    const required = surface.required.includes(key) ? "required" : "optional";
    if (key === "member_id") parts.push(`member (${required})`);
    if (key === "exit_id") parts.push(`exit request (${required})`);
    if (key === "declaration_id") parts.push(`declaration (${required})`);
  }
  if (surface.allowed.includes("date_from") || surface.allowed.includes("date_to")) {
    parts.push("date range (optional)");
  }
  return `Scope: ${parts.join(", ")}.`;
}

function statusPill(status: string) {
  if (status === "completed") {
    return (
      <Pill bg="emeraldSoft" color="emerald">
        Completed
      </Pill>
    );
  }
  if (status === "failed") {
    return (
      <Pill bg="brickSoft" color="brick">
        Failed
      </Pill>
    );
  }
  if (status === "queued") {
    return (
      <Pill bg="goldSoft" color="navy">
        Queued
      </Pill>
    );
  }
  // An unrecognised status is still the server's fact — inert text.
  return <Pill>{status}</Pill>;
}

/** One witnessed export row: requester-only refresh, the on-screen
 * view (the prototype's "look at it now" job) + downloads. */
function ExportRow({
  record,
  onView,
}: Readonly<{ record: ExportOut; onView: (record: ExportOut, artifact: Artifact) => void }>) {
  const refresh = useMutation({
    mutationFn: () => fetchExport(record.id),
    onSuccess: (fresh) => {
      recordWitnessedExport(fresh);
      announce("Export status refreshed.");
    },
    onError: () => {
      announce("The export status could not be refreshed.");
    },
  });

  const download = useMutation({
    mutationFn: ({ path, filename }: { path: string; filename: string }) =>
      downloadArtifact(path, filename),
    onSuccess: () => {
      announce("Export downloaded.");
    },
    onError: () => {
      announce("The export download failed.");
    },
  });

  const artifact = record.artifact;
  // the filename is built ONLY from sanitized parts — the stem is
  // the recognised report enum value (or a fixed fallback; never the raw
  // server string), the date part is shape-checked (the boundary already
  // asserts it, — this keeps the SINK safe by construction), and
  // the extension is appended last and fixed.
  const stem = exportFilenameStem(record.report);
  const asOfSlice = record.as_of.slice(0, 10);
  const asOfDate = DATE_RE.test(asOfSlice) ? asOfSlice : "undated";

  return (
    <tr>
      <td>
        <div>{reportLabel(record.report)}</div>
        <div className={styles.mono}>{record.id}</div>
      </td>
      <td className={styles.muted}>{fmtDateTime(record.created_at)}</td>
      <td className={styles.muted}>{fmtDateTime(record.as_of)}</td>
      <td>{statusPill(record.status)}</td>
      <td className={styles.muted}>
        {artifact === null
          ? "—"
          : artifact.truncated
            ? `${artifact.row_count} (truncated at the server cap of ${artifact.row_limit})`
            : `${artifact.row_count}`}
      </td>
      <td>
        <div className={styles.rowActions}>
          <Button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
            {refresh.isPending ? "Refreshing…" : "Refresh"}
          </Button>
          {artifact !== null && (
            <>
              <Button type="button" onClick={() => onView(record, artifact)}>
                View
              </Button>
              <Button
                type="button"
                onClick={() =>
                  download.mutate({
                    path: artifact.csv_download,
                    filename: `${stem}-${asOfDate}.csv`,
                  })
                }
                disabled={download.isPending}
              >
                CSV
              </Button>
              <Button
                type="button"
                onClick={() =>
                  download.mutate({
                    path: artifact.pdf_download,
                    filename: `${stem}-${asOfDate}.pdf`,
                  })
                }
                disabled={download.isPending}
              >
                PDF
              </Button>
            </>
          )}
        </div>
        {artifact !== null && (
          <div className={styles.formNote}>
            Download links expire {fmtDateTime(artifact.expires_at)}.
          </div>
        )}
        {refresh.isError && <ErrorBanner error={refresh.error} />}
        {download.isError && <ErrorBanner error={download.error} />}
      </td>
    </tr>
  );
}

export function ReportsScreen() {
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const witnessed = useWitnessedExports();

  return (
    <div>
      <div className={styles.toolbar}>
        <div>
          <div className={styles.toolbarTitle}>Reports &amp; exports</div>
          <div className={styles.toolbarNote}>
            Every report renders SERVER-side (CSV + PDF) from one consistent
            snapshot, with role-based columns, a hard row cap and expiring
            download links — nothing is computed in this screen.
          </div>
        </div>
        <div className={styles.toolbarActions}>
          <Button type="button" variant="primary" onClick={() => setDrawer({ mode: "queue" })}>
            Run export queue
          </Button>
        </div>
      </div>

      <div className={grid.cards4}>
        {REPORT_NAMES.map((report) => (
          <Card key={report} className={`${grid.half} ${styles.reportCard}`}>
            <div className={styles.reportTitle}>{REPORT_LABELS[report]}</div>
            <div className={styles.reportMeta}>{scopeLine(report)}</div>
            <div className={styles.cardActions}>
              <Button type="button" onClick={() => setDrawer({ mode: "request", report })}>
                Request export…
              </Button>
            </div>
          </Card>
        ))}
      </div>

      <div className={styles.sectionGap}>
        <Card padded={false}>
          <div className={styles.exportsHead}>
            <span>Exports requested this session</span>
            <span className={styles.exportsNote}>
              The contract has no export list endpoint — only exports
              requested in THIS tab appear here; every export stays in the
              backend audit trail regardless
            </span>
          </div>
          {witnessed.length === 0 ? (
            <div className={styles.exportsEmpty}>
              No exports requested in this session yet. Pick a report above
              to request one.
            </div>
          ) : (
            <div className={styles.exportsTableWrap}>
              <table className={styles.exportsTable}>
                <thead>
                  <tr>
                    <th scope="col">Report</th>
                    <th scope="col">Requested</th>
                    <th scope="col">As of</th>
                    <th scope="col">Status</th>
                    <th scope="col">Rows</th>
                    <th scope="col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {witnessed.map((record) => (
                    <ExportRow
                      key={record.id}
                      record={record}
                      onView={(viewRecord, artifact) =>
                        setDrawer({ mode: "view", record: viewRecord, artifact })
                      }
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {drawer !== null && drawer.mode === "request" && (
        <RequestExportDrawer report={drawer.report} onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "queue" && (
        <RunQueueDialog onClose={() => setDrawer(null)} />
      )}
      {drawer !== null && drawer.mode === "view" && (
        <ViewExportDrawer
          record={drawer.record}
          artifact={drawer.artifact}
          onClose={() => setDrawer(null)}
        />
      )}
    </div>
  );
}
