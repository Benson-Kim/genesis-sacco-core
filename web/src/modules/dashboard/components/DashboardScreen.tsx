"use client";

/**
 *  dashboard — live figures from GET /dashboard/summary (/).
 *
 * Money rules: every amount below is the API's decimal
 * string rendered through the string-only formatters — the client never
 * computes, aggregates or converts a money figure. Slices the role is
 * not granted arrive omitted and render as "—" (deny-by-default, 1.6).
 */
import { useQuery } from "@tanstack/react-query";
import { Card, Stat } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { fmtAmount, fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchDashboardSummary } from "../api";
import type { DashboardSummary } from "../schemas";
import {
    ClassificationBars,
    FlowsBarChart,
    PerformingDonut,
    Sparkline,
} from "./DashboardCharts";
import grid from "@/modules/layout/grid.module.css";
import styles from "./DashboardScreen.module.css";
import charts from "./DashboardCharts.module.css";

export const DASHBOARD_QUERY_KEY = ["dashboard", "summary"] as const;

export function useDashboardSummary() {
    // Composite read model: a server-computed snapshot with a rendered
    // as_of stamp — entity-class staleTime from lib/query.ts.
    return useQuery({
        queryKey: DASHBOARD_QUERY_KEY,
        queryFn: fetchDashboardSummary,
        staleTime: STALE_TIME.composite,
    });
}

// Single copy of the KES label lives in fmtKes (reuse-first; finding).
function kes(value: string | undefined): string {
    return value === undefined ? "—" : fmtKes(value);
}

/**
 * Per-KPI trend sparkline: the SERVER's
 * share-of-window-peak geometry consumed VERBATIM — the polyline maps
 * the pct integers 1:1; no client summing, interpolation or derived
 * deltas exist here.
 * - series === null/undefined is PER-GRANT STRUCTURAL WITHHOLDING
 *   (the KPI's parent grant is missing or the slice predates the
 *   expand): NOTHING mounts — no affordance ever claims data exists.
 * - A granted but SHORT series (a young tenant; the server caps at 12
 *   points and never pads) renders the honest absence copy instead of
 *   a degenerate line.
 */
function KpiTrendSparkline({
    series,
    stroke,
    testId,
    caption,
}: Readonly<{
    series: readonly { pct: number }[] | null | undefined;
    stroke: "gold" | "navy";
    testId: string;
    caption: string;
}>) {
    if (series === null || series === undefined) return null;
    if (series.length < 2) {
        return <div className={charts.sparkCaption}>Not enough monthly history to chart.</div>;
    }
    return (
        <>
            <Sparkline values={series.map((p) => p.pct)} stroke={stroke} testId={testId} />
            <div className={charts.sparkCaption}>{caption}</div>
        </>
    );
}

function StatCards({ summary }: Readonly<{ summary: DashboardSummary }>) {
    // KPI sparklines: SERVER-normalized series from the
    // charts slice — mapped to plain integer arrays here, no money
    // value is ever read. HONEST labelling: the deposit/disbursement
    // flow series render on the cards they truly describe; the PAR-30
    // and member cards consume the ledger-(k) kpi_trends
    // slice (posting-history / status-fact truth) VERBATIM.
    const flowSeries = summary.charts?.flows ?? null;
    const depositTrend = flowSeries?.months.map((m) => m.deposits_pct) ?? null;
    const disbursementTrend = flowSeries?.months.map((m) => m.disbursements_pct) ?? null;
    return (
        <>
            <Card>
                <Stat
                    label="Active members"
                    value={summary.members ? String(summary.members.active_members) : "—"}
                />
                <KpiTrendSparkline
                    series={summary.kpi_trends?.members}
                    stroke="navy"
                    testId="sparkline-members"
                    caption="active members, month-end"
                />
            </Card>
            <Card>
                <Stat label="Deposits" value={kes(summary.deposits?.total_deposits)} />
                {depositTrend !== null && depositTrend.length > 1 && (
                    <>
                        <Sparkline values={depositTrend} stroke="gold" />
                        <div className={charts.sparkCaption}>monthly deposit flows</div>
                    </>
                )}
            </Card>
            <Card>
                <Stat label="Loan book" value={kes(summary.loan_book?.outstanding_balance)} />
                {disbursementTrend !== null && disbursementTrend.length > 1 && (
                    <>
                        <Sparkline values={disbursementTrend} stroke="navy" />
                        <div className={charts.sparkCaption}>monthly disbursements</div>
                    </>
                )}
            </Card>
            <Card>
                <Stat
                    label="PAR-30"
                    value={summary.loan_book ? `${summary.loan_book.par30_ratio_pct}%` : "—"}
                />
                <KpiTrendSparkline
                    series={summary.kpi_trends?.par30}
                    stroke="gold"
                    testId="sparkline-par30"
                    caption="PAR-30 ratio, month-end"
                />
            </Card>
        </>
    );
}

function FlowsCard({
    flows,
    chart,
}: Readonly<{
    flows: DashboardSummary["monthly_flows"];
    chart: DashboardSummary["charts"];
}>) {
    if (flows === null || flows === undefined) return null;
    const flowsChart = chart?.flows ?? null;
    return (
        <Card className={grid.twoThirds}>
            <h2 className={styles.title}>Deposits vs disbursements</h2>
            {flowsChart !== null && <FlowsBarChart flows={flowsChart} />}
        </Card>
    );
}

function PortfolioQualityCard({
    chart,
}: Readonly<{ chart: DashboardSummary["charts"] }>) {
    const portfolio = chart?.portfolio ?? null;
    if (portfolio === null) return null;
    return (
        <Card className={grid.third}>
            <h2 className={styles.title}>Portfolio quality</h2>
            <PerformingDonut portfolio={portfolio} />
        </Card>
    );
}

function PipelineCard({ pipeline }: Readonly<{ pipeline: DashboardSummary["pipeline"] }>) {
    if (pipeline === null || pipeline === undefined) return null;
    return (
        <Card className={grid.third}>
            <h2 className={styles.title}>Application pipeline</h2>
            <ul className={styles.pipeline}>
                {pipeline.map((stage) => (
                    <li key={stage.stage} className={styles.pipelineRow}>
                        <span className={styles.stageName}>{stage.stage}</span>
                        <span className={styles.stageCount}>{stage.count}</span>
                    </li>
                ))}
                {pipeline.length === 0 && <li className={styles.muted}>No applications in flight.</li>}
            </ul>
        </Card>
    );
}

function ClassificationCard({
    loanBook,
    chart,
}: Readonly<{
    loanBook: DashboardSummary["loan_book"];
    chart: DashboardSummary["charts"];
}>) {
    if (loanBook === null || loanBook === undefined) return null;
    const portfolio = chart?.portfolio ?? null;
    return (
        <Card className={grid.third}>
            <h2 className={styles.title}>Portfolio classification</h2>
            {/* Progress-bar supplement: server-computed
                shares; the table below is the figures of record. */}
            {portfolio !== null && <ClassificationBars portfolio={portfolio} />}
            <table className={styles.flows}>
                <thead>
                    <tr>
                        <th scope="col" className={styles.th}>
                            Classification
                        </th>
                        <th scope="col" className={styles.thRight}>
                            Loans
                        </th>
                        <th scope="col" className={styles.thRight}>
                            Balance
                        </th>
                    </tr>
                </thead>
                <tbody>
                    {loanBook.by_classification.map((slice) => (
                        <tr key={slice.classification}>
                            <td className={styles.td}>{slice.classification}</td>
                            <td className={styles.tdRight}>{slice.count}</td>
                            <td className={styles.tdRight}>{fmtAmount(slice.balance)}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </Card>
    );
}

function MembersByTypeCard({
  members,
  deposits,
}: Readonly<{
  members: DashboardSummary["members"];
  deposits: DashboardSummary["deposits"];
}>) {
  if (members === null || members === undefined) return null;

  // Descending by count, not API order — makes the scale gap (165 vs 1)
  // read immediately instead of requiring the eye to hunt for the max.
  const sorted = [...members.by_type].sort((a, b) => b.active - a.active);
  const maxActive = Math.max(1, ...sorted.map((t) => t.active)); // avoid /0

  return (
    <Card className={grid.third}>
      <h2 className={styles.title}>Members by type</h2>

      <div className={styles.heroKpi}>
        <span className={styles.heroKpiLabel}>Total share capital</span>
        <span className={styles.heroKpiValue}>{kes(deposits?.total_share_capital)}</span>
      </div>

      <ul className={styles.memberBarList}>
        {sorted.map((memberType) => (
          <li key={memberType.type} className={styles.memberBarRow}>
            <span className={styles.memberBarLabel}>{memberType.type}</span>
            <div className={styles.memberBarTrack}>
              <div
                className={styles.memberBarFill}
                style={{ width: `${(memberType.active / maxActive) * 100}%` }}
              />
            </div>
            <span className={styles.memberBarValue}>{memberType.active}</span>
          </li>
        ))}
        {sorted.length === 0 && <li className={styles.muted}>No members recorded yet.</li>}
      </ul>
    </Card>
  );
}

export function DashboardScreen() {
    const summary = useDashboardSummary();

    if (summary.isPending) {
        return <div className={styles.muted}>Loading dashboard…</div>;
    }

    if (summary.isError) {
        return <ErrorBanner error={summary.error} />;
    }

    return (
        // Shared responsive grid (grid.module.css): 4 -> 3 -> 2 -> 1 columns.
        <div className={grid.cards4}>
            <StatCards summary={summary.data} />

            <div className={`${grid.cards3} ${grid.wide}`}>
                <FlowsCard flows={summary.data.monthly_flows} chart={summary.data.charts} />
                <PortfolioQualityCard chart={summary.data.charts} />
            </div>
            <div className={`${grid.cards3} ${grid.wide}`}>
                <PipelineCard pipeline={summary.data.pipeline} />
                <ClassificationCard
                    loanBook={summary.data.loan_book}
                    chart={summary.data.charts}
                />
                <MembersByTypeCard
                    members={summary.data.members}
                    deposits={summary.data.deposits}
                />
            </div>
            <div className={styles.asOf}>As of {fmtDateTime(summary.data.as_of)}</div>
        </div>
    );
}
