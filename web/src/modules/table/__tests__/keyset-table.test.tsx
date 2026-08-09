import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { z } from "zod";
import { KeysetTable, type Column } from "../KeysetTable";
import { keysetPageSchema, type KeysetPage } from "../schemas";
import { useKeysetList } from "../useKeysetList";

interface Row {
  id: string;
  name: string;
}

const PAGES: Record<string, { items: Row[]; next_cursor: string | null }> = {
  first: {
    items: [
      { id: "GP-0001", name: "Amina Odhiambo" },
      { id: "GP-0002", name: "Brian Mwangi" },
    ],
    next_cursor: "cursor-2",
  },
  "cursor-2": {
    items: [{ id: "GP-0003", name: "Cynthia Wairimu" }],
    next_cursor: null,
  },
};

const rowSchema = z.object({ id: z.string(), name: z.string() });
const pageSchema = keysetPageSchema(rowSchema);

async function fetchPage(cursor: string | null): Promise<KeysetPage<Row>> {
  const raw = PAGES[cursor ?? "first"];
  if (raw === undefined) throw new Error("unknown cursor");
  return pageSchema.parse(raw);
}

const columns: Column<Row>[] = [
  { key: "id", header: "Member no", render: (row) => row.id },
  { key: "name", header: "Name", render: (row) => row.name },
];

function Harness({ onRowClick }: Readonly<{ onRowClick?: (row: Row) => void }>) {
  const query = useKeysetList<Row>({ queryKey: ["test-rows"], fetchPage });
  return (
    <KeysetTable columns={columns} query={query} rowKey={(row) => row.id} onRowClick={onRowClick} />
  );
}

function renderHarness(onRowClick?: (row: Row) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Harness onRowClick={onRowClick} />
    </QueryClientProvider>,
  );
}

describe("KeysetTable + useKeysetList", () => {
  it("renders the first page and loads the next via the server cursor", async () => {
    renderHarness();

    expect(await screen.findByText("Amina Odhiambo")).toBeInTheDocument();
    expect(screen.getByText("Brian Mwangi")).toBeInTheDocument();
    expect(screen.queryByText("Cynthia Wairimu")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Cynthia Wairimu")).toBeInTheDocument();
    // Last page has next_cursor null — the load-more control disappears.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument(),
    );
  });

  it("row drill-down works by mouse AND keyboard (issue #8 a11y)", async () => {
    const clicks: string[] = [];
    renderHarness((row) => clicks.push(row.id));

    const row = (await screen.findByText("Amina Odhiambo")).closest("tr");
    expect(row).not.toBeNull();
    await userEvent.click(row as HTMLElement);
    // Keyboard activation: rows are focusable buttons for drill-down.
    (row as HTMLElement).focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard(" ");
    expect(clicks).toEqual(["GP-0001", "GP-0001", "GP-0001"]);
  });

  it("rows without onRowClick stay inert (no phantom button semantics)", async () => {
    renderHarness();
    const row = (await screen.findByText("Amina Odhiambo")).closest("tr");
    expect(row).not.toHaveAttribute("role");
    expect(row).not.toHaveAttribute("tabindex");
  });

  it("the table sits in a keyboard-focusable labelled scroll region (P15 Phase B)", async () => {
    renderHarness();
    await screen.findByText("Amina Odhiambo");
    const region = screen.getByRole("region", { name: "Table" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region.querySelector("table")).not.toBeNull();
  });

  it("validates the page contract with zod", () => {
    expect(() =>
      pageSchema.parse({ items: [{ id: 1, name: "bad" }], next_cursor: null }),
    ).toThrow();
    expect(pageSchema.parse({ items: [], next_cursor: null })).toEqual({
      items: [],
      nextCursor: null,
    });
  });
});
