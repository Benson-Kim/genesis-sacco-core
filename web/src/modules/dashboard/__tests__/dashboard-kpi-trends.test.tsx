/**
 * #31 batch 8, ledger (k) — per-KPI trend sparklines on the dashboard
 * (issue #32's PAR-30 + members cards).
 *
 * These suites prove, falsifiably:
 *  - the Zod boundary REJECTS numeric ratios, float/out-of-range
 *    geometry and missing point keys;
 *  - the sparklines consume the SERVER series VERBATIM — the fixture
 *    is DELIBERATELY NON-ADDITIVE: the server pct integers are NOT
 *    what any client recomputation (share-of-peak from ratio_pct or
 *    active counts, summing, interpolation, deltas) would yield, and
 *    the geometry a re-deriving client would draw is asserted ABSENT;
 *  - per-grant STRUCTURAL withholding: an omitted series mounts
 *    NOTHING (no affordance claims data exists);
 *  - honest absence: a granted but short series renders copy, never a
 *    degenerate line.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { DashboardScreen } from "../components/DashboardScreen";
import { dashboardSummarySchema, kpiTrendsSchema } from "../schemas";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { GET: jest.fn() },
}));

const mockGet = api.GET as unknown as jest.Mock;

function res(status: number): Response {
  return { status } as Response;
}

/**
 * DELIBERATELY NON-ADDITIVE series (the consume-VERBATIM rule):
 * - par30: share-of-peak recomputed CLIENT-side from the ratio
 *   strings ("3.20","5.00","1.10" — peak 5.00) would yield 64/100/22;
 *   the server says 41/97/13 (its own Decimal window geometry). A
 *   client that re-derived would draw polyline "0,36 50,0 100,78" —
 *   asserted ABSENT; the verbatim drawing is "0,59 50,3 100,87".
 * - members: share-of-peak from the active counts (80/100/90 — peak
 *   100) would yield 80/100/90; the server says 55/88/31. Re-derived
 *   polyline "0,20 50,0 100,10" is asserted ABSENT; verbatim is
 *   "0,45 50,12 100,69".
 * (Polyline y = 100 - pct; x = index * 100 / lastIndex — index
 * arithmetic only, hand-computed here in the test.)
 */
const KPI_TRENDS = {
  par30: [
    { month: "2026-06", ratio_pct: "3.20", pct: 41 },
    { month: "2026-07", ratio_pct: "5.00", pct: 97 },
    { month: "2026-08", ratio_pct: "1.10", pct: 13 },
  ],
  members: [
    { month: "2026-06", active: 80, pct: 55 },
    { month: "2026-07", active: 100, pct: 88 },
    { month: "2026-08", active: 90, pct: 31 },
  ],
};

const SUMMARY = {
  as_of: "2026-08-05T08:00:00+00:00",
  members: { active_members: 90, by_type: [] },
  deposits: { total_deposits: "1234567.10", total_share_capital: "250000.00" },
  loan_book: {
    active_loans: 12,
    outstanding_balance: "8000.00",
    npl_balance: "2000.00",
    npl_ratio_pct: "25.00",
    par30_ratio_pct: "1.10",
    par30_balance: "88.00",
    provisions: "60.00",
    penalties_due: "0",
    by_classification: [],
  },
  guarantors: null,
  monthly_flows: [],
  pipeline: [],
  kpi_trends: KPI_TRENDS,
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

describe("kpi_trends contract (Zod — server-computed series ONLY)", () => {
  it("accepts the slice, per-grant omissions and pre-expand payloads", () => {
    expect(kpiTrendsSchema.safeParse(KPI_TRENDS).success).toBe(true);
    expect(kpiTrendsSchema.safeParse({}).success).toBe(true);
    expect(kpiTrendsSchema.safeParse({ par30: null, members: null }).success).toBe(true);
    expect(dashboardSummarySchema.safeParse(SUMMARY).success).toBe(true);
    const withoutTrends: Record<string, unknown> = { ...SUMMARY };
    delete withoutTrends["kpi_trends"];
    expect(dashboardSummarySchema.safeParse(withoutTrends).success).toBe(true);
  });

  it("REJECTS a numeric ratio_pct — the ratio is a str(Decimal) STRING end-to-end", () => {
    const drifted = {
      par30: [{ month: "2026-06", ratio_pct: 3.2, pct: 41 }],
      members: null,
    };
    expect(kpiTrendsSchema.safeParse(drifted).success).toBe(false);
  });

  it("REJECTS float, negative and >100 geometry and a missing point key (accept/reject matrix)", () => {
    const withPar30Pct = (pct: unknown) =>
      kpiTrendsSchema.safeParse({
        par30: [{ month: "2026-06", ratio_pct: "3.20", pct }],
        members: null,
      }).success;
    expect(withPar30Pct(0)).toBe(true);
    expect(withPar30Pct(100)).toBe(true);
    expect(withPar30Pct(41.5)).toBe(false);
    expect(withPar30Pct(-1)).toBe(false);
    expect(withPar30Pct(101)).toBe(false);
    expect(withPar30Pct("41")).toBe(false);
    // A point missing its accessible figure of record is a violation.
    expect(
      kpiTrendsSchema.safeParse({
        par30: null,
        members: [{ month: "2026-06", pct: 55 }],
      }).success,
    ).toBe(false);
  });
});

describe("KPI sparklines — verbatim server series, never derived", () => {
  it("draws BOTH sparklines from the server pct integers 1:1; re-derived geometry is ABSENT (non-additive fixture)", async () => {
    mockGet.mockResolvedValue({ data: SUMMARY, error: undefined, response: res(200) });
    const { container } = renderScreen();

    const par30 = await screen.findByTestId("sparkline-par30");
    // VERBATIM: y = 100 - server pct (41/97/13), hand-computed.
    expect(par30.querySelector("polyline")).toHaveAttribute("points", "0,59 50,3 100,87");

    const members = screen.getByTestId("sparkline-members");
    // VERBATIM: server pcts 55/88/31, hand-computed.
    expect(members.querySelector("polyline")).toHaveAttribute("points", "0,45 50,12 100,69");

    // FALSIFIABLE never-derived proof: what client math WOULD draw
    // from the ratios (64/100/22) or the active counts (80/100/90)
    // must not exist anywhere in the document.
    const html = container.innerHTML;
    expect(html).not.toContain("0,36 50,0 100,78");
    expect(html).not.toContain("0,20 50,0 100,10");

    // The accessible figures of record stay the card texts —
    // par30_ratio_pct and active_members render verbatim beside the
    // geometry, never re-scaled.
    expect(screen.getByText("1.10%")).toBeInTheDocument();
    expect(screen.getByText("90")).toBeInTheDocument();

    // The geometry container is a supplement, never a carrier (a11y).
    expect(par30).toHaveAttribute("aria-hidden", "true");
    expect(members).toHaveAttribute("aria-hidden", "true");
  });

  it("per-grant STRUCTURAL withholding: an omitted series mounts NOTHING — no sparkline, no absence copy claiming data exists", async () => {
    // Wire reality (response_model_exclude_none + the backend
    // per-role grant suite): the ungranted series KEY IS ABSENT —
    // the server omits it, never null/[]. The fixture mirrors that.
    mockGet.mockResolvedValue({
      data: { ...SUMMARY, kpi_trends: { par30: KPI_TRENDS.par30 } },
      error: undefined,
      response: res(200),
    });
    renderScreen();
    expect(await screen.findByTestId("sparkline-par30")).toBeInTheDocument();
    expect(screen.queryByTestId("sparkline-members")).not.toBeInTheDocument();
    expect(screen.queryByText("active members, month-end")).not.toBeInTheDocument();
    // Exactly ONE absence-copy candidate may exist and it belongs to
    // no card here: the withheld series renders nothing at all.
    expect(screen.queryByText("Not enough monthly history to chart.")).not.toBeInTheDocument();
    // ZERO extra wire probes (review R6): the withheld series never
    // triggers a follow-up fetch — the single summary GET is the ONLY
    // wire call the screen makes.
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/dashboard/summary");
  });

  it("a wholly omitted kpi_trends slice (pre-expand payload) mounts neither sparkline", async () => {
    const withoutTrends: Record<string, unknown> = { ...SUMMARY };
    delete withoutTrends["kpi_trends"];
    mockGet.mockResolvedValue({ data: withoutTrends, error: undefined, response: res(200) });
    renderScreen();
    expect(await screen.findByText("PAR-30")).toBeInTheDocument();
    expect(screen.queryByTestId("sparkline-par30")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sparkline-members")).not.toBeInTheDocument();
    // ZERO extra wire probes here too: the pre-expand payload never
    // provokes a retry or a per-series fetch.
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith("/dashboard/summary");
  });

  it("honest absence: a granted but SHORT series renders the copy, never a degenerate line", async () => {
    mockGet.mockResolvedValue({
      data: {
        ...SUMMARY,
        kpi_trends: {
          par30: [{ month: "2026-08", ratio_pct: "1.10", pct: 100 }],
          members: KPI_TRENDS.members,
        },
      },
      error: undefined,
      response: res(200),
    });
    renderScreen();
    expect(
      await screen.findByText("Not enough monthly history to chart."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("sparkline-par30")).not.toBeInTheDocument();
    expect(screen.getByTestId("sparkline-members")).toBeInTheDocument();
  });
});
