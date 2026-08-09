import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RequireModule } from "@/modules/authz/components/RequireModule";
import { api } from "@/lib/api";
import { memberSchema } from "../schemas";
import { MembersScreen } from "../components/MembersScreen";

jest.mock("@/lib/api", () => ({
    api: { GET: jest.fn(), POST: jest.fn() },
}));

const mockGet = api.GET as unknown as jest.Mock;
const mockPost = api.POST as unknown as jest.Mock;

const FULL_PERMISSIONS = {
    role_id: "role-admin",
    permissions: [
        { module: "members", can_view: true, can_create: true, can_edit: true, can_approve: true },
    ],
};

const VIEW_ONLY_PERMISSIONS = {
    role_id: "role-viewer",
    permissions: [
        { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
    ],
};

const MEMBER = {
    id: "9b2f6c1e-0000-4000-8000-000000000001",
    member_no: "GP-0001",
    type: "person",
    name: "Amina Odhiambo",
    phone: "+254700000001",
    email: "amina@example.com",
    status: "active",
    version: 1,
    branch_id: null,
    dividend_payout: null,
};

// DELIBERATELY NON-ADDITIVE aggregate fixture (#31 batch 3 review): no
// figure equals any combination of the others, so a register that
// summed or derived a figure would surface a string asserted ABSENT.
const AGGREGATES = {
    deposits_total: "1000.11",
    shares_total: "200.22",
    loans_outstanding: "300.33",
    guarantees_pledged: "40.04",
};

const MEMBER_WITH_AGGREGATES = { ...MEMBER, aggregates: AGGREGATES };

/**
 * Minimal Response stand-in: toApiError reads ONLY `.status` and jsdom has
 * no fetch globals (same pattern as contract-helpers.test.ts).
 */
function res(status: number): Response {
    return { status } as Response;
}

function ok(data: unknown) {
    return { data, error: undefined, response: res(200) };
}

function wirePermissions(payload: unknown) {
    mockGet.mockImplementation((path: string, options?: unknown) => {
        if (path === "/me/permissions") return Promise.resolve(ok(payload));
        if (path === "/members") {
            // The wire mirrors the server contract (#31 batch 3
            // review): rows carry aggregates ONLY when the request
            // opted in with include=aggregates.
            const query = (options as { params?: { query?: Record<string, unknown> } } | undefined)
                ?.params?.query;
            if (query?.["include"] === "aggregates")
                return Promise.resolve(
                    ok({ items: [MEMBER_WITH_AGGREGATES], next_cursor: null }),
                );
            return Promise.resolve(ok({ items: [MEMBER], next_cursor: null }));
        }
        return Promise.reject(new Error(`unexpected GET ${path}`));
    });
}

function renderScreen(children: React.ReactNode) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

afterEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
});

describe("members contract (Zod — FM: contract violation)", () => {
    it("REJECTS an unknown status/type instead of rendering it", () => {
        expect(memberSchema.safeParse(MEMBER).success).toBe(true);
        expect(memberSchema.safeParse({ ...MEMBER, status: "vip" }).success).toBe(false);
        expect(memberSchema.safeParse({ ...MEMBER, type: "trust" }).success).toBe(false);
    });
});

describe("route guard (FM: deny-by-default)", () => {
    it("denies when /me/permissions errors — nothing privileged rendered", async () => {
        mockGet.mockImplementation((path: string) => {
            if (path === "/me/permissions")
                return Promise.resolve({
                    data: undefined,
                    error: { category: "forbidden", correlation_id: "c-9" },
                    response: res(403),
                });
            return Promise.resolve(ok({ items: [MEMBER], next_cursor: null }));
        });
        renderScreen(
            <RequireModule module="members">
                <MembersScreen />
            </RequireModule>,
        );
        expect(await screen.findByText("Access denied")).toBeInTheDocument();
        expect(screen.queryByText("Amina Odhiambo")).not.toBeInTheDocument();
    });
});

describe("MembersScreen", () => {
    it("lists members via the keyset contract — cursor/limit only, no offsets; the register OPTS IN to aggregates with include=aggregates", async () => {
        wirePermissions(FULL_PERMISSIONS);
        renderScreen(<MembersScreen />);
        expect(await screen.findByText("Amina Odhiambo")).toBeInTheDocument();
        const listCall = mockGet.mock.calls.find(([path]) => path === "/members");
        expect(listCall).toBeDefined();
        const query = (listCall?.[1] as { params: { query: Record<string, unknown> } }).params
            .query;
        expect(Object.keys(query).sort()).toEqual([
            "cursor",
            "include",
            "limit",
            "status",
            "type",
        ]);
        expect(query["include"]).toBe("aggregates");
        expect(query).not.toHaveProperty("offset");
        expect(query).not.toHaveProperty("page");
    });

    it("renders the four server aggregate strings VERBATIM per row and NEVER sums them — the non-additive fixture proves no derived figure exists (#31 batch 3 review)", async () => {
        wirePermissions(FULL_PERMISSIONS);
        renderScreen(<MembersScreen />);
        expect(await screen.findByText("Amina Odhiambo")).toBeInTheDocument();
        // All four SERVER strings, byte-identical (no KES prefix, no
        // grouping, no re-formatting).
        expect(await screen.findByText("1000.11")).toBeInTheDocument();
        expect(screen.getByText("200.22")).toBeInTheDocument();
        expect(screen.getByText("300.33")).toBeInTheDocument();
        expect(screen.getByText("40.04")).toBeInTheDocument();
        // Hand-computed HERE, in the test, from the fixture: deposits
        // with shares would read 1200.33; a grand figure across all
        // four would read 1540.70; deposits net of loans would read
        // 699.78. None may exist anywhere on the register.
        expect(screen.queryByText("1200.33")).toBeNull();
        expect(screen.queryByText("1540.70")).toBeNull();
        expect(screen.queryByText("699.78")).toBeNull();
    });

    it("renders a hostile aggregate string byte-identical as inert TEXT (never a parser sink)", async () => {
        const hostileFigure = "<img src=x onerror=window.__pwned=7>";
        mockGet.mockImplementation((path: string, options?: unknown) => {
            if (path === "/me/permissions") return Promise.resolve(ok(FULL_PERMISSIONS));
            if (path === "/members") {
                const query = (
                    options as { params?: { query?: Record<string, unknown> } } | undefined
                )?.params?.query;
                if (query?.["include"] === "aggregates")
                    return Promise.resolve(
                        ok({
                            items: [
                                {
                                    ...MEMBER_WITH_AGGREGATES,
                                    aggregates: { ...AGGREGATES, deposits_total: hostileFigure },
                                },
                            ],
                            next_cursor: null,
                        }),
                    );
                return Promise.resolve(ok({ items: [MEMBER], next_cursor: null }));
            }
            return Promise.reject(new Error(`unexpected GET ${path}`));
        });
        const { container } = renderScreen(<MembersScreen />);
        expect(await screen.findByText(hostileFigure)).toBeInTheDocument();
        expect(container.querySelector("img")).toBeNull();
        expect(container.querySelector("script")).toBeNull();
    });

    it("hides the register affordance without can_create (UI never offers what the API forbids)", async () => {
        wirePermissions(VIEW_ONLY_PERMISSIONS);
        renderScreen(<MembersScreen />);
        expect(await screen.findByText("Amina Odhiambo")).toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Register member" })).not.toBeInTheDocument();
    });

    it("double-submit fires exactly ONE create call (FM: replayed mutation)", async () => {
        wirePermissions(FULL_PERMISSIONS);
        let resolveCreate: (value: unknown) => void = () => undefined;
        mockPost.mockImplementation(
            () =>
                new Promise((resolve) => {
                    resolveCreate = resolve;
                }),
        );
        const user = userEvent.setup();
        renderScreen(<MembersScreen />);
        await user.click(await screen.findByRole("button", { name: "Register member" }));
        // Once the drawer is open, the toolbar trigger and the form submit
        // share an accessible name — scope to the dialog to hit the submit.
        const dialog = await screen.findByRole("dialog");
        await user.type(within(dialog).getByLabelText("Full name"), "Brian Mwangi");
        const submit = within(dialog).getByRole("button", { name: "Register member" });
        await user.click(submit);
        // Second click while pending: button is disabled AND the handler
        // short-circuits — exactly one POST.
        await user.click(submit);
        await user.click(submit);
        expect(mockPost).toHaveBeenCalledTimes(1);
        const [, options] = mockPost.mock.calls[0] as [
            string,
            { headers: Record<string, string>; body: Record<string, unknown> },
        ];
        expect(options.headers["Idempotency-Key"]).toMatch(/[0-9a-f-]{36}/);
        resolveCreate(ok({ ...MEMBER, id: "9b2f6c1e-0000-4000-8000-000000000002", member_no: "GP-0002", name: "Brian Mwangi" }));
        expect(await screen.findByText("Member GP-0002 registered.")).toBeInTheDocument();
    });

    it("retrying an identical failed submission REUSES the Idempotency-Key; editing rotates it", async () => {
        wirePermissions(FULL_PERMISSIONS);
        mockPost.mockResolvedValue({
            data: undefined,
            error: { category: "internal_error", correlation_id: "c-1" },
            response: res(500),
        });
        const user = userEvent.setup();
        renderScreen(<MembersScreen />);
        await user.click(await screen.findByRole("button", { name: "Register member" }));
        const dialog = await screen.findByRole("dialog");
        await user.type(within(dialog).getByLabelText("Full name"), "Cynthia Wairimu");
        const submit = within(dialog).getByRole("button", { name: "Register member" });
        await user.click(submit);
        await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
        await user.click(submit);
        await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
        const keyOf = (call: unknown[]) =>
            (call[1] as { headers: Record<string, string> }).headers["Idempotency-Key"];
        expect(keyOf(mockPost.mock.calls[1])).toBe(keyOf(mockPost.mock.calls[0]));
        // Content change = new logical intent = new key.
        await user.type(screen.getByLabelText("Full name"), " Jr");
        await user.click(submit);
        await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(3));
        expect(keyOf(mockPost.mock.calls[2])).not.toBe(keyOf(mockPost.mock.calls[0]));
    });

    it("renders 409 as the inline conflict banner — no silent retry, no data echo", async () => {
        wirePermissions(FULL_PERMISSIONS);
        mockPost.mockResolvedValue({
            data: undefined,
            error: { category: "conflict", correlation_id: "corr-409" },
            response: res(409),
        });
        const user = userEvent.setup();
        renderScreen(<MembersScreen />);
        await user.click(await screen.findByRole("button", { name: "Register member" }));
        const dialog = await screen.findByRole("dialog");
        await user.type(within(dialog).getByLabelText("Full name"), "Duplicate Person");
        await user.click(within(dialog).getByRole("button", { name: "Register member" }));
        const alert = await screen.findByRole("alert");
        expect(alert).toHaveTextContent("changed or conflicts");
        expect(alert).toHaveTextContent("corr-409");
        expect(mockPost).toHaveBeenCalledTimes(1);
    });
});

