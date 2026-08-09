/**
 * Issue #32 (#31 batch 5, U3+X4) — dashboard chart supplements.
 *
 * The RECORDED design decision (route (a)): geometry percentages are
 * SERVER-computed integers; the client renders scale only. These
 * suites prove, falsifiably:
 *  - the Zod boundary REJECTS non-integer / out-of-range geometry;
 *  - every rendered figure is a VERBATIM server value (a deliberately
 *    NON-ADDITIVE fixture: client-derived percentages/sums are
 *    asserted ABSENT document-wide);
 *  - permission-stripped charts degrade to the tables (no fabricated
 *    geometry, no crash);
 *  - empty/zero states render honest copy, never a fabricated ring or
 *    full-height bar;
 *  - hostile label strings render as inert text (XSS).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { DashboardScreen } from "../components/DashboardScreen";
import { dashboardChartsSchema, dashboardSummarySchema } from "../schemas";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { GET: jest.fn() },
}));

const mockGet = api.GET as unknown as jest.Mock;

function res(status: number): Response {
  return { status } as Response;
}

/**
 * DELIBERATELY NON-ADDITIVE fixture (the never-summed rule): the
 * monthly_flows AMOUNTS do not reproduce the chart percentages under
 * any client-side math — deposits 123.45 vs 800.00 would be 15%, the
 * server says 40; disbursements 800.00 is the peak (100). A client
 * that re-derives geometry (or sums the window) would render 15, 25,
 * "923.45" or "1,046.90" — all asserted ABSENT.
 */
const CHART_SUMMARY = {
  as_of: "2026-08-05T08:00:00+00:00",
  members: { active_members: 87, by_type: [] },
  deposits: { total_deposits: "1234567.10", total_share_capital: "250000.00" },
  loan_book: {
    active_loans: 12,
    outstanding_balance: "8000.00",
    npl_balance: "2000.00",
    npl_ratio_pct: "25.00",
    par30_ratio_pct: "3.20",
    par30_balance: "256.00",
    provisions: "60.00",
    penalties_due: "0",
    by_classification: [
      { classification: "normal", count: 8, balance: "5000.00", provisions: "50.00" },
      { classification: "watch", count: 3, balance: "1000.00", provisions: "5.00" },
      { classification: "loss", count: 1, balance: "2000.00", provisions: "5.00" },
    ],
  },
  guarantors: null,
  monthly_flows: [
    { month: "2026-06", deposits: "123.45", disbursements: "800.00" },
    { month: "2026-07", deposits: "-50.00", disbursements: "0" },
  ],
  pipeline: [{ stage: "Review", count: 4 }],
  charts: {
    flows: {
      months: [
        { month: "2026-06", deposits_pct: 40, disbursements_pct: 100 },
        { month: "2026-07", deposits_pct: 0, disbursements_pct: 0 },
      ],
      axis_max: "800.00",
    },
    portfolio: {
      performing_pct: 75,
      npl_pct: 25,
      classification: [
        { classification: "normal", pct: 63 },
        { classification: "watch", pct: 13 },
        { classification: "loss", pct: 25 },
      ],
    },
  },
};

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DashboardScreen />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  mockGet.mockReset();
});

describe("charts contract (Zod — geometry is server-computed integers ONLY)", () => {
  const base = CHART_SUMMARY.charts;

  it("accepts the server geometry (and nullish sub-slices)", () => {
    expect(dashboardChartsSchema.safeParse(base).success).toBe(true);
    expect(dashboardChartsSchema.safeParse({}).success).toBe(true);
    expect(dashboardChartsSchema.safeParse({ flows: null, portfolio: null }).success).toBe(
      true,
    );
    expect(dashboardSummarySchema.safeParse(CHART_SUMMARY).success).toBe(true);
    // The charts slice stays OPTIONAL: pre-expand payloads still parse.
    const withoutCharts: Record<string, unknown> = { ...CHART_SUMMARY };
    delete withoutCharts["charts"];
    expect(dashboardSummarySchema.safeParse(withoutCharts).success).toBe(true);
  });

  it("REJECTS floats, negatives, >100 and string percentages (accept/reject matrix)", () => {
    const withPct = (deposits_pct: unknown) =>
      dashboardChartsSchema.safeParse({
        ...base,
        flows: {
          ...base.flows,
          months: [{ month: "2026-06", deposits_pct, disbursements_pct: 10 }],
        },
      }).success;
    expect(withPct(0)).toBe(true);
    expect(withPct(100)).toBe(true);
    expect(withPct(12.5)).toBe(false); // float geometry is a violation
    expect(withPct(-1)).toBe(false);
    expect(withPct(101)).toBe(false);
    expect(withPct("40")).toBe(false); // stringly-typed pct rejected
    const withDonut = (performing_pct: unknown) =>
      dashboardChartsSchema.safeParse({
        ...base,
        portfolio: { ...base.portfolio, performing_pct },
      }).success;
    expect(withDonut(101)).toBe(false);
    expect(withDonut(99.9)).toBe(false);
  });

  it("REJECTS a garbage axis_max — the scale caption feeds fmtKes verbatim", () => {
    const withAxis = (axis_max: unknown) =>
      dashboardChartsSchema.safeParse({
        ...base,
        flows: { ...base.flows, axis_max },
      }).success;
    expect(withAxis("800.00")).toBe(true);
    expect(withAxis("0")).toBe(true); // the SQL bare zero of a silent window
    for (const bad of ["abc", "1e5", "007.10", "-800.00", 800, ""]) {
      expect(withAxis(bad)).toBe(false);
    }
  });
});

describe("chart rendering — verbatim server geometry, never derived", () => {
  it("renders bars/donut/classification from the SERVER integers; client-derived figures are ABSENT document-wide", async () => {
    mockGet.mockResolvedValue({ data: CHART_SUMMARY, error: undefined, response: res(200) });
    const { container } = renderScreen();

    // Grouped bars: heights are EXACTLY the server percentages.
    expect(await screen.findByTestId("flows-bar-chart")).toBeInTheDocument();
    expect(screen.getByTestId("bar-dep-2026-06")).toHaveStyle({ height: "40%" });
    expect(screen.getByTestId("bar-dis-2026-06")).toHaveStyle({ height: "100%" });
    // The negative (reversal-only) month clamps to 0 SERVER-side; the
    // signed figure still renders verbatim in the table.
    expect(screen.getByTestId("bar-dep-2026-07")).toHaveStyle({ height: "0%" });
    expect(screen.getByRole("cell", { name: "-50.00" })).toBeInTheDocument();

    // Scale caption: the server's axis_max VERBATIM through fmtKes.
    expect(screen.getByText(/Bar scale: 0 – KES 800\.00/)).toBeInTheDocument();

    // Donut: server integers as text; ring is aria-hidden geometry.
    expect(screen.getByTestId("performing-donut")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getAllByText("75%").length).toBeGreaterThanOrEqual(1);
    // "25%" legitimately renders TWICE from the fixture: the donut's
    // npl_pct stat AND the loss classification's pct text.
    expect(screen.getAllByText("25%").length).toBeGreaterThanOrEqual(2);

    // Classification bars: name + pct TEXT (colour-never-alone) with
    // widths exactly the server share.
    expect(screen.getByTestId("cls-fill-normal")).toHaveStyle({ width: "63%" });
    expect(screen.getByText("13%")).toBeInTheDocument();

    // KPI sparklines mounted for the two honest series only.
    expect(screen.getByTestId("sparkline-gold")).toBeInTheDocument();
    expect(screen.getByTestId("sparkline-navy")).toBeInTheDocument();

    // NEVER-DERIVED proof (falsifiable): what client math WOULD yield
    // from the non-additive fixture must not exist anywhere —
    // 123.45/800.00 = 15%; window sums 923.45 / 1,046.90; the
    // recomputed npl share of the by_classification sum (2000/8000
    // = 25 matches here by design, so probe the flows side which
    // cannot): no "15%", no summed totals, no re-scaled bar.
    const html = container.innerHTML;
    expect(html).not.toContain("15%");
    expect(html).not.toContain("923.45");
    expect(html).not.toContain("1,046.90");
    expect(html).not.toContain("946.90");
  });

  it("permission-stripped charts: tables render, no geometry, no crash", async () => {
    const withoutCharts = { ...CHART_SUMMARY, charts: undefined };
    mockGet.mockResolvedValue({ data: withoutCharts, error: undefined, response: res(200) });
    renderScreen();
    // The tables (the figures of record) are untouched…
    expect(await screen.findByText("Monthly flows")).toBeInTheDocument();
    expect(screen.getByText("Portfolio classification")).toBeInTheDocument();
    // …and NO chart geometry is fabricated client-side.
    expect(screen.queryByTestId("flows-bar-chart")).not.toBeInTheDocument();
    expect(screen.queryByTestId("performing-donut")).not.toBeInTheDocument();
    expect(screen.queryByTestId("classification-bars")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sparkline-gold")).not.toBeInTheDocument();
    expect(screen.queryByText("Portfolio quality")).not.toBeInTheDocument();
  });

  it("honest empty states: a silent window and an empty book chart NOTHING", async () => {
    mockGet.mockResolvedValue({
      data: {
        ...CHART_SUMMARY,
        charts: {
          flows: {
            months: [
              { month: "2026-06", deposits_pct: 0, disbursements_pct: 0 },
              { month: "2026-07", deposits_pct: 0, disbursements_pct: 0 },
            ],
            axis_max: "0",
          },
          portfolio: { performing_pct: 0, npl_pct: 0, classification: [] },
        },
      },
      error: undefined,
      response: res(200),
    });
    renderScreen();
    expect(
      await screen.findByText("No flows to chart for this window."),
    ).toBeInTheDocument();
    expect(screen.getByText("No active loan book to chart.")).toBeInTheDocument();
    expect(screen.queryByTestId("flows-bar-chart")).not.toBeInTheDocument();
    expect(screen.queryByTestId("performing-donut")).not.toBeInTheDocument();
  });

  it("hostile classification labels render as inert TEXT and never pick a token (XSS + CSS-injection inertness)", async () => {
    const hostile = '<img src=x onerror="window.__pwned=1"> var(--evil)';
    mockGet.mockResolvedValue({
      data: {
        ...CHART_SUMMARY,
        charts: {
          ...CHART_SUMMARY.charts,
          portfolio: {
            performing_pct: 75,
            npl_pct: 25,
            classification: [{ classification: hostile, pct: 10 }],
          },
        },
      },
      error: undefined,
      response: res(200),
    });
    const { container } = renderScreen();
    // The hostile string renders as TEXT (React interpolation)…
    expect(await screen.findByText(hostile)).toBeInTheDocument();
    // …no element was injected and no script sink executed…
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // …and the fill fell back to the code-owned neutral token: the
    // hostile value was never interpolated into CSS.
    const fill = screen.getByTestId(`cls-fill-${hostile}`);
    expect(fill).toHaveStyle({ background: "var(--navyMid)" });
    // The hostile value never reached the style attribute either.
    expect(fill.getAttribute("style")).not.toContain("--evil");
  });
});
