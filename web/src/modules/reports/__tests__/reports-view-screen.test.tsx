/**
 * On-screen report view (P15 batch 5, U2 — ViewExportDrawer): the
 * prototype's "look at it now" job restored over the EXISTING download
 * route (zero contract change).
 *
 * Falsifiable proofs:
 *  - every cell renders the SERVER's artifact VERBATIM; a NON-ADDITIVE
 *    fixture pins that no summed/derived figure exists anywhere;
 *  - truncation facts (artifact metadata + X-Export-Truncated header)
 *    surface verbatim;
 *  - the presentation cap points at the canonical download and quotes
 *    the SERVER row count, never a client count;
 *  - a malformed artifact is an honest error — no mis-split figures;
 *  - hostile cells are inert text (XSS);
 *  - loading/error states render.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MAX_RENDER_ROWS, ViewExportDrawer } from "../components/ViewExportDrawer";
import type { Artifact, ExportOut } from "../schemas";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  api: { GET: jest.fn() },
}));

const mockGet = api.GET as unknown as jest.Mock;

const CSV_TOKEN = "csv-Tok_en-1";

/** DELIBERATELY NON-ADDITIVE money cells: 100.10 + 200.30 = 300.40 —
 * asserted ABSENT (nothing sums the artifact client-side). */
const CSV_BODY = "Member No,Type,Amount\r\nGP-001,Deposit,100.10\r\nGP-002,Deposit,200.30\r\n";

function artifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    row_count: 2,
    truncated: false,
    row_limit: 10000,
    expires_at: "2026-08-05T12:00:00+00:00",
    csv_download: `/exports/downloads/${CSV_TOKEN}`,
    pdf_download: "/exports/downloads/pdf-Tok_en-1",
    ...overrides,
  };
}

function record(): ExportOut {
  return {
    id: "eeeeeeee-1111-2222-3333-444444444444",
    report: "portfolio_at_risk_aging",
    status: "completed",
    filters: {},
    allowed_columns: ["member_no", "type", "amount"],
    as_of: "2026-08-04T10:00:00+00:00",
    completed_at: "2026-08-04T10:05:00+00:00",
    created_at: "2026-08-04T10:00:00+00:00",
    artifact: artifact(),
  };
}

interface StreamResponseInit {
  ok?: boolean;
  status?: number;
  text?: string;
  truncated?: string | null;
  limit?: string | null;
}

/** Minimal Response stand-in for the parseAs:"stream" call — the api
 * layer reads ok/status, text() and the two truncation headers. */
function streamResponse({
  ok = true,
  status = 200,
  text = CSV_BODY,
  truncated = "false",
  limit = "10000",
}: StreamResponseInit = {}) {
  return {
    ok,
    status,
    text: async () => text,
    json: async () => ({ category: "not_found", correlation_id: "corr-view" }),
    headers: {
      get: (name: string) =>
        name === "X-Export-Truncated" ? truncated : name === "X-Export-Limit" ? limit : null,
    },
  };
}

function renderDrawer(rec: ExportOut = record(), art: Artifact = artifact()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ViewExportDrawer record={rec} artifact={art} onClose={() => undefined} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  mockGet.mockReset();
});

describe("ViewExportDrawer", () => {
  it("renders every server cell VERBATIM and never derives a figure (non-additive fixture)", async () => {
    mockGet.mockResolvedValue({ response: streamResponse() });
    const { container } = renderDrawer();

    // The wire call is the CONTRACT's download route with the token as
    // a path parameter (asserted at the mocked generated client).
    expect(await screen.findByText("GP-001")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet.mock.calls[0]![0]).toBe("/exports/downloads/{token}");
    expect(mockGet.mock.calls[0]![1]).toMatchObject({
      params: { path: { token: CSV_TOKEN } },
    });

    // Column titles are the SERVER's header row.
    expect(screen.getByText("Member No")).toBeInTheDocument();
    // Money cells byte-identical — never re-formatted, never KES-labelled
    // by this screen (the artifact is the truth, verbatim).
    expect(screen.getByText("100.10")).toBeInTheDocument();
    expect(screen.getByText("200.30")).toBeInTheDocument();
    // NEVER-SUMMED (falsifiable): the client-computable total is absent.
    expect(container.innerHTML).not.toContain("300.40");
    // Complete-artifact note quotes the SERVER row count + header verbatim.
    expect(
      screen.getByText(/Complete artifact: 2 rows \(X-Export-Truncated: false\)/),
    ).toBeInTheDocument();
    // The canonical-download honesty note is always present.
    expect(screen.getByText(/downloads remain the canonical artifact/)).toBeInTheDocument();
  });

  it("surfaces SERVER truncation verbatim (metadata + header)", async () => {
    mockGet.mockResolvedValue({ response: streamResponse({ truncated: "true" }) });
    renderDrawer(record(), artifact({ truncated: true, row_count: 10000 }));
    expect(
      await screen.findByText(
        /Server truncation: this artifact holds the first 10000 rows \(server cap 10000; X-Export-Truncated: true\)/,
      ),
    ).toBeInTheDocument();
  });

  it("caps the on-screen render at MAX_RENDER_ROWS with an honest pointer at the download", async () => {
    const manyRows = Array.from({ length: MAX_RENDER_ROWS + 5 }, (_, i) => `row-${i},x`).join(
      "\r\n",
    );
    mockGet.mockResolvedValue({
      response: streamResponse({ text: `H1,H2\r\n${manyRows}\r\n` }),
    });
    renderDrawer(record(), artifact({ row_count: MAX_RENDER_ROWS + 5 }));
    expect(await screen.findByText("row-0")).toBeInTheDocument();
    expect(screen.getByText(`row-${MAX_RENDER_ROWS - 1}`)).toBeInTheDocument();
    // Rows beyond the presentation cap do NOT render…
    expect(screen.queryByText(`row-${MAX_RENDER_ROWS}`)).not.toBeInTheDocument();
    // …and the note says so, pointing at the canonical artifact.
    expect(
      screen.getByText(
        new RegExp(
          `Showing the first ${MAX_RENDER_ROWS} rows on screen — download the CSV for the full artifact`,
        ),
      ),
    ).toBeInTheDocument();
  });

  it("a MALFORMED artifact is an honest error — no mis-split figure ever renders", async () => {
    mockGet.mockResolvedValue({
      response: streamResponse({ text: 'A,B\r\n"150.00"7,x\r\n' }),
    });
    renderDrawer();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    // Neither the broken figure nor any table cell rendered.
    expect(screen.queryByText(/150\.00/)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("hostile artifact cells render as inert TEXT (XSS)", async () => {
    // The payload carries quotes, so on the wire it must ride as a
    // QUOTED cell with doubled quotes (the parser's own strict
    // grammar — a bare quote in an unquoted cell rightly THROWS).
    const hostile = '<img src=x onerror="window.__pwnedView=1">';
    const wireCell = `"${hostile.replaceAll('"', '""')}"`;
    mockGet.mockResolvedValue({
      response: streamResponse({ text: `A\r\n${wireCell}\r\n` }),
    });
    const { container } = renderDrawer();
    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(
      (window as unknown as { __pwnedView?: number }).__pwnedView,
    ).toBeUndefined();
  });

  it("an expired link surfaces the least-disclosure banner; nothing is retried", async () => {
    mockGet.mockResolvedValue({ response: streamResponse({ ok: false, status: 404 }) });
    renderDrawer();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the loading state while the artifact streams", () => {
    mockGet.mockReturnValue(new Promise(() => undefined));
    renderDrawer();
    expect(screen.getByText("Loading the rendered report…")).toBeInTheDocument();
  });

  it("an artifact with only a header row renders the honest empty copy", async () => {
    mockGet.mockResolvedValue({ response: streamResponse({ text: "H1,H2\r\n" }) });
    renderDrawer(record(), artifact({ row_count: 0 }));
    expect(
      await screen.findByText("The rendered report has no data rows."),
    ).toBeInTheDocument();
  });
});
