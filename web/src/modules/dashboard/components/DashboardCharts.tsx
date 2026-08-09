"use client";

/**
 * Dashboard chart supplements (the prototype's KPI sparklines, the grouped deposit and disbursement bars, performing donut and classification progress bars).
 *
 * RECORDED DESIGN DECISION (route (a)): every percentage here is
 * a SERVER-computed integer from the charts slice of
 * GET /dashboard/summary — normalized backend-side in Decimal inside
 * the same snapshot as the sibling figures. This module performs NO
 * money math: the only arithmetic below is SVG
 * coordinate spacing over list INDEXES and pre-normalized 0..100
 * integers; every rendered money/label string is a verbatim server
 * value through the string-only formatters.
 *
 * A11y (constraint): charts are SUPPLEMENTS, never sole carriers —
 * each geometry container is aria-hidden and the sibling Stat text /
 * tables remain the accessible truth; colour is never the only signal
 * (class names and percent TEXT always accompany a bar). Hand-rolled
 * SVG/CSS within the test-locked design tokens — NO third-party chart
 * library (the client-hygiene gate bans third-party requests).
 */
import { fmtKes } from "@/lib/format";
import type { FlowsChart, PortfolioChart } from "../schemas";
import styles from "./DashboardCharts.module.css";

/** Code-owned classification → token map (never derived from server
 * data): unknown classifications fall back to the neutral navyMid, so
 * a hostile value can only ever pick the default — it is NEVER
 * interpolated into CSS. Mirrors the prototype's class palette. */
const CLASS_TOKEN: Record<string, string> = {
    normal: "var(--emerald)",
    watch: "var(--orange)",
    substandard: "var(--gold)",
    doubtful: "var(--brick)",
    loss: "var(--loss)",
};

function classToken(classification: string): string {
    return CLASS_TOKEN[classification] ?? "var(--navyMid)";
}

/**
 * KPI sparkline: a polyline over server-normalized 0..100 integers.
 * The x spacing is index arithmetic (never a money value); y is the
 * server's percentage subtracted from the viewBox height.
 */
export function Sparkline({
    values,
    stroke,
    testId,
}: Readonly<{ values: readonly number[]; stroke: "gold" | "navy"; testId?: string }>) {
    if (values.length < 2) return null;
    const last = values.length - 1;
    const points = values.map((v, i) => `${(i * 100) / last},${100 - v}`).join(" ");
    return (
        <svg
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            className={styles.sparkline}
            aria-hidden="true"
            data-testid={testId ?? `sparkline-${stroke}`}
        >
            <polyline
                points={points}
                className={stroke === "gold" ? styles.sparkGold : styles.sparkNavy}
                fill="none"
                vectorEffect="non-scaling-stroke"
            />
        </svg>
    );
}

/**
 * Grouped deposit and disbursement bars (CSS heights from the
 * server's percentages). The bar geometry is aria-hidden; the sibling
 * "Monthly flows" table renders the signed figures of record. The
 * scale caption renders the SERVER's axis_max verbatim.
 */
export function FlowsBarChart({ flows }: Readonly<{ flows: FlowsChart }>) {
    const silent = flows.axis_max === "0" || flows.months.length === 0;
    if (silent) {
        return <div className={styles.emptyNote}>No flows to chart for this window.</div>;
    }
    return (
        <div>
            <div className={styles.barChart} aria-hidden="true" data-testid="flows-bar-chart">
                {flows.months.map((m) => (
                    <div key={m.month} className={styles.barGroup}>
                        <div className={styles.barPair}>
                            <div
                                className={styles.barDeposits}
                                style={{ height: `${m.deposits_pct}%` }}
                                data-testid={`bar-dep-${m.month}`}
                            />
                            <div
                                className={styles.barDisbursements}
                                style={{ height: `${m.disbursements_pct}%` }}
                                data-testid={`bar-dis-${m.month}`}
                            />
                        </div>
                        <div className={styles.barMonth}>{m.month}</div>
                    </div>
                ))}
            </div>
            <div className={styles.legendRow}>
                <span className={styles.legendItem}>
                    <span className={`${styles.legendSwatch} ${styles.swatchGold}`} /> Deposits
                </span>
                <span className={styles.legendItem}>
                    <span className={`${styles.legendSwatch} ${styles.swatchNavy}`} /> Disbursed
                </span>
                <span className={styles.scaleNote}>
                    Bar scale: 0 – {fmtKes(flows.axis_max)} (server-computed; reversal-only
                    months clamp to zero — the signed truth is in the table)
                </span>
            </div>
        </div>
    );
}

/**
 * Performing donut (conic-gradient from the server's integer). An
 * empty book (0/0 — the server's honest empty state) renders copy,
 * never a fabricated ring. The percent TEXT beside the ring is the
 * accessible carrier.
 */
export function PerformingDonut({ portfolio }: Readonly<{ portfolio: PortfolioChart }>) {
    const empty = portfolio.performing_pct === 0 && portfolio.npl_pct === 0;
    if (empty) {
        return <div className={styles.emptyNote}>No active loan book to chart.</div>;
    }
    return (
        <div className={styles.donutWrap}>
            <div
                className={styles.donut}
                style={{
                    background: `conic-gradient(var(--gold) ${portfolio.performing_pct}%, var(--brickSoft) 0)`,
                }}
                aria-hidden="true"
                data-testid="performing-donut"
            >
                <div className={styles.donutHole}>
                    <span className={styles.donutPct}>{portfolio.performing_pct}%</span>
                    <span className={styles.donutCaption}>performing</span>
                </div>
            </div>
            <div className={styles.donutStats}>
                <div>
                    Performing <strong>{portfolio.performing_pct}%</strong>
                </div>
                <div>
                    Non-performing <strong>{portfolio.npl_pct}%</strong>
                </div>
                <div className={styles.scaleNote}>
                    Server-computed shares of the outstanding book — the classification
                    table carries the figures of record.
                </div>
            </div>
        </div>
    );
}

/**
 * Classification progress bars: name + percent TEXT always accompany
 * the bar (colour-never-alone); the track geometry is aria-hidden.
 */
export function ClassificationBars({ portfolio }: Readonly<{ portfolio: PortfolioChart }>) {
    if (portfolio.classification.length === 0) return null;
    return (
        <div className={styles.clsBars} data-testid="classification-bars">
            {portfolio.classification.map((share) => (
                <div key={share.classification} className={styles.clsRow}>
                    <span className={styles.clsName}>{share.classification}</span>
                    <span className={styles.clsPct}>{share.pct}%</span>
                    <div className={styles.clsTrack} aria-hidden="true">
                        <div
                            className={styles.clsFill}
                            style={{
                                width: `${share.pct}%`,
                                background: classToken(share.classification),
                            }}
                            data-testid={`cls-fill-${share.classification}`}
                        />
                    </div>
                </div>
            ))}
        </div>
    );
}
