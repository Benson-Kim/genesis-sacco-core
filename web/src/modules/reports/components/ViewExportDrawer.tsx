"use client";

/**
 * On-screen report view (restores the prototype's repShell/repPortfolio/repStatement "look at it now" job).
 *
 * ZERO contract change: the drawer fetches the SAME server-rendered
 * CSV artifact the download button streams (the capability-token
 * route, generated client, headers-only auth) and renders it as a
 * read-only table.
 *
 * HONESTY RULES (the mandate):
 * - Every cell is the SERVER's rendered artifact VERBATIM — parsed by
 *   the strict RFC 4180 parser (csv.ts) and rendered as inert React
 *   text. Nothing is summed, derived, re-formatted or re-labelled; a
 *   malformed artifact surfaces as an honest error, never mis-split
 *   figures.
 * - Truncation is surfaced verbatim: the artifact's server metadata
 *   (row_count/truncated/row_limit) plus the X-Export-Truncated /
 *   X-Export-Limit response headers.
 * - The DOWNLOAD path stays canonical: very large artifacts render
 *   only the first MAX_RENDER_ROWS rows on screen with an explicit
 *   note pointing at the CSV/PDF downloads (a presentation cap, not a
 *   re-computation — the server row_count is quoted, never counted
 *   client-side beyond the rendered slice).
 * - Read-only surface: light dismissal is intentional (the exit- statement drawer precedent, X7) — nothing can be lost.
 */
import { useQuery } from "@tanstack/react-query";
import { Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { fmtDateTime } from "@/lib/format";
import { fetchArtifactText, type ArtifactText } from "../api";
import { parseCsv } from "../csv";
import { reportLabel, type Artifact, type ExportOut } from "../schemas";
import styles from "./Reports.module.css";

/** Presentation cap for the on-screen table (the artifact itself is
 * already hard-capped server-side at the export row limit). */
export const MAX_RENDER_ROWS = 200;

interface ParsedArtifact {
  headers: string[];
  rows: string[][];
  meta: ArtifactText;
}

async function loadArtifact(csvDownload: string): Promise<ParsedArtifact> {
  const meta = await fetchArtifactText(csvDownload);
  // Strict parse (csv.ts): throws on malformed input — surfaced by the
  // query error state below, never rendered as mis-split figures.
  const grid = parseCsv(meta.text);
  const [headers, ...rows] = grid;
  return { headers: headers ?? [], rows, meta };
}

export function ViewExportDrawer({
  record,
  artifact,
  onClose,
}: Readonly<{ record: ExportOut; artifact: Artifact; onClose: () => void }>) {
  const view = useQuery({
    queryKey: ["reports", "artifact-view", record.id],
    queryFn: () => loadArtifact(artifact.csv_download),
    // The artifact is immutable once rendered; its link simply expires.
    staleTime: Infinity,
    retry: 0,
  });

  const shown = view.data === undefined ? [] : view.data.rows.slice(0, MAX_RENDER_ROWS);
  const capApplied = view.data !== undefined && view.data.rows.length > shown.length;

  return (
    <Modal title={`${reportLabel(record.report)} — on-screen view`} onClose={onClose}>
      <div className={styles.formNote}>
        Rendered {fmtDateTime(record.as_of)} server-side from one consistent
        snapshot with role-based columns. Every cell below is the server&apos;s
        rendered export VERBATIM — nothing is computed, summed or re-labelled
        in this screen. The CSV/PDF downloads remain the canonical artifact.
      </div>

      {view.isPending && <div className={styles.exportsEmpty}>Loading the rendered report…</div>}
      {view.isError && <ErrorBanner error={view.error} />}

      {view.data !== undefined && (
        <>
          {/* Truncation facts VERBATIM: server metadata + headers. */}
          <div className={styles.formNote}>
            {artifact.truncated
              ? `Server truncation: this artifact holds the first ${artifact.row_count} rows (server cap ${artifact.row_limit}; X-Export-Truncated: ${view.data.meta.truncatedHeader ?? "—"}).`
              : `Complete artifact: ${artifact.row_count} rows (X-Export-Truncated: ${view.data.meta.truncatedHeader ?? "—"}).`}
            {capApplied &&
              ` Showing the first ${MAX_RENDER_ROWS} rows on screen — download the CSV for the full artifact.`}
          </div>
          {shown.length === 0 ? (
            <div className={styles.exportsEmpty}>The rendered report has no data rows.</div>
          ) : (
            <div className={styles.exportsTableWrap}>
              <table className={styles.exportsTable}>
                <thead>
                  <tr>
                    {view.data.headers.map((header, headerIndex) => (
                      // The server's own column titles; index keys are
                      // stable for an immutable artifact.
                      <th scope="col" key={headerIndex}>
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((row, rowIndex) => (
                    // Index keys: the parsed artifact is immutable.
                    <tr key={rowIndex}>
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex}>{cell}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
