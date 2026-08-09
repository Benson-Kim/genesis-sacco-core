import { z } from "zod";
import {
    aggregateMoneySchema,
    isoTimestampSchema,
    signedAggregateMoneySchema,
} from "@/lib/schemas";

/**
 * Zod validation of GET /dashboard/summary (composite endpoint).
 * Money values are decimal STRINGS end-to-end — never coerced to
 * numbers in the client.
 *
 * The endpoint omits (nulls) every slice the caller's role is not
 * granted — the UI renders "—" for missing slices and never fabricates
 * figures (deny-by-default, least disclosure).
 *
 * MONEY SHAPES (retrofit): every figure here is a SQL
 * aggregate (application/dashboard.py / loans.py — COALESCE(SUM(...),
 * 0) and friends), so the shared AGGREGATE shapes apply: the canonical
 * two-place decimal OR the bare "0" an empty aggregate serialises to.
 * The SIGNED aggregate shape is taken ONLY where the backend documents
 * a negative branch: the monthly flow series (reversal rows subtract
 * in the month they were POSTED — MONTHLY_FLOWS_SQL) and
 * free_capacity (an ADVISORY "balance less pledged" difference read
 * without the P9 locks). Everything else REJECTS a '-'.
 */
export const memberTypeCountSchema = z.object({
    type: z.string(),
    total: z.number().int(),
    active: z.number().int(),
});

export const membersOverviewSchema = z.object({
    active_members: z.number().int(),
    by_type: z.array(memberTypeCountSchema),
});

export const depositTotalsSchema = z.object({
    /** DEPOSIT_TOTAL_SQL and SHARE_TOTAL_SQL — COALESCE(SUM(balance),
     * 0) over CHECK (balance >= 0) columns: non-negative aggregates,
     * fmtKes-fed (retrofit). */
    total_deposits: aggregateMoneySchema,
    total_share_capital: aggregateMoneySchema,
});

export const classificationSliceSchema = z.object({
    classification: z.string(),
    count: z.number().int(),
    /** Grouped COALESCE(SUM(...), 0) aggregates (loans.py
     * portfolio_summary), fed to fmtAmount (retrofit). */
    balance: aggregateMoneySchema,
    provisions: aggregateMoneySchema,
});

export const portfolioSummarySchema = z.object({
    active_loans: z.number().int(),
    /** COALESCE(SUM(...), 0) aggregates — bare "0" on an empty book,
     * two-place otherwise; fmtKes-fed (retrofit). The ratio
     * percentages are rendered as "%" text, never fmtKes-fed —
     * deliberately left unasserted (sweep note). */
    outstanding_balance: aggregateMoneySchema,
    npl_balance: aggregateMoneySchema,
    npl_ratio_pct: z.string(),
    par30_balance: aggregateMoneySchema,
    par30_ratio_pct: z.string(),
    provisions: aggregateMoneySchema,
    penalties_due: aggregateMoneySchema,
    by_classification: z.array(classificationSliceSchema),
});

export const guarantorCapacitySchema = z.object({
    member_id: z.string(),
    member_no: z.string(),
    name: z.string(),
    /** live_pledged_total — a COALESCE'd SUM of CHECK (amount > 0)
     * pledges: non-negative aggregate, fmtKes-fed (retrofit). */
    pledged_total: aggregateMoneySchema,
    /** ADVISORY "balance less pledged" difference (dashboard.py
     * GuarantorCapacity), read without the P9 locks — legitimately
     * NEGATIVE when the balance dropped after pledging or no deposit
     * account exists; fmtKes-fed with its sign (retrofit). */
    free_capacity: signedAggregateMoneySchema,
});

export const guarantorAggregatesSchema = z.object({
    active_guarantees: z.number().int(),
    /** GUARANTEE_TOTALS_SQL — COALESCE(SUM(amount), 0) over
     * CHECK (amount > 0) pledges: non-negative aggregate, fmtKes-fed
     * (retrofit). */
    total_pledged: aggregateMoneySchema,
    distinct_guarantors: z.number().int(),
    guarantors: z.array(guarantorCapacitySchema),
});

export const monthlyFlowSchema = z.object({
    /** "YYYY-MM" UTC calendar month key — rendered verbatim, never
     * fmtDateTime-fed: deliberately left unasserted (sweep
     * note). */
    month: z.string(),
    /** MONTHLY_FLOWS_SQL — reversal rows SUBTRACT in the month they
     * were posted, so a reversal-only month legitimately nets
     * negative; zero-filled silent months arrive as "0.00" and an
     * all-other-types month as the SQL bare "0"; fed to fmtAmount
     * with the sign (retrofit). */
    deposits: signedAggregateMoneySchema,
    disbursements: signedAggregateMoneySchema,
});

export const pipelineStageSchema = z.object({
    stage: z.string(),
    count: z.number().int(),
});

/**
 * CHART GEOMETRY (the RECORDED design decision route (a)):
 * every percentage below is SERVER-computed (Decimal, ROUND_HALF_UP,
 * application/dashboard.py scale_pct) from figures inside the same
 * REPEATABLE READ snapshot as the sibling slices. The client renders
 * these integers as visual scale ONLY — no client-side money math
 * exists anywhere (the money rule stays absolute); the sibling
 * monthly_flows / loan_book slices remain the figures of record and
 * the accessible truth (a11y: colour-never-alone).
 *
 * Integers 0..100 are ASSERTED at this boundary: a float, a negative
 * or an out-of-range value is a contract violation and is REJECTED —
 * it would silently distort the geometry.
 */
const pctIntSchema = z.number().int().min(0).max(100);

export const flowBarScaleSchema = z.object({
    /** Same "YYYY-MM" key as the sibling monthly_flows row. */
    month: z.string(),
    deposits_pct: pctIntSchema,
    disbursements_pct: pctIntSchema,
});

export const flowsChartSchema = z.object({
    months: z.array(flowBarScaleSchema),
    /** The common scale ceiling — a SERVER aggregate string rendered
     * verbatim as the chart's scale caption (never re-derived). */
    axis_max: aggregateMoneySchema,
});

export const classificationShareSchema = z.object({
    classification: z.string(),
    pct: pctIntSchema,
});

export const portfolioChartSchema = z.object({
    performing_pct: pctIntSchema,
    npl_pct: pctIntSchema,
    classification: z.array(classificationShareSchema),
});

export const dashboardChartsSchema = z.object({
    /** Follows transactions:view (the monthly_flows parent grant). */
    flows: flowsChartSchema.nullish(),
    /** Follows loan_book:view (the portfolio-summary parent grant). */
    portfolio: portfolioChartSchema.nullish(),
});

/**
 * PER-KPI TREND SERIES: server-computed
 * month-end points, capped at 12, from posting-history / audit-fact
 * truth inside the same REPEATABLE READ snapshot. The client consumes
 * the series VERBATIM: pct is share-of-window-peak geometry computed
 * SERVER-side (Decimal) — no client summing, interpolation or derived
 * deltas exist anywhere; ratio_pct/active are the
 * accessible figures of record and are never re-scaled. Each series
 * follows its KPI card's parent grant (par30 <- loan_book:view,
 * members <- members:view) and is OMITTED when ungranted — an absent
 * series mounts NOTHING (structural withholding, least disclosure).
 */
export const par30TrendPointSchema = z.object({
    /** "YYYY-MM" month key — rendered verbatim if surfaced. */
    month: z.string(),
    /** str(Decimal) ratio recomputed from posting-history truth at
     * the month-end cutoff (never the mutable snapshot columns) — a
     * STRING end-to-end; a numeric value is a contract violation. */
    ratio_pct: z.string(),
    pct: pctIntSchema,
});

export const membersTrendPointSchema = z.object({
    month: z.string(),
    /** Count reconstructed from the immutable member.status
     * transition facts at the cutoff. */
    active: z.number().int(),
    pct: pctIntSchema,
});

export const kpiTrendsSchema = z.object({
    par30: z.array(par30TrendPointSchema).nullish(),
    members: z.array(membersTrendPointSchema).nullish(),
});

export const dashboardSummarySchema = z.object({
    /** datetime.isoformat() (api/dashboard.py summary.as_of) — feeds
     * fmtDateTime in the header: garbage is REJECTED, never
     * "Invalid Date" (an earlier review lesson; retrofit). */
    as_of: isoTimestampSchema,
    members: membersOverviewSchema.nullish(),
    deposits: depositTotalsSchema.nullish(),
    loan_book: portfolioSummarySchema.nullish(),
    guarantors: guarantorAggregatesSchema.nullish(),
    monthly_flows: z.array(monthlyFlowSchema).nullish(),
    pipeline: z.array(pipelineStageSchema).nullish(),
    charts: dashboardChartsSchema.nullish(),
    kpi_trends: kpiTrendsSchema.nullish(),
});

export type DashboardSummary = z.infer<typeof dashboardSummarySchema>;
export type MonthlyFlow = z.infer<typeof monthlyFlowSchema>;
export type PipelineStage = z.infer<typeof pipelineStageSchema>;
export type ClassificationSlice = z.infer<typeof classificationSliceSchema>;
export type DashboardCharts = z.infer<typeof dashboardChartsSchema>;
export type FlowsChart = z.infer<typeof flowsChartSchema>;
export type PortfolioChart = z.infer<typeof portfolioChartSchema>;
export type KpiTrends = z.infer<typeof kpiTrendsSchema>;

