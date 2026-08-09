/**
 * Registration wizard presentation legs — the design flow's four steps
 * over the REAL screens. Falsifiable (oracles hand-computed):
 * - FM1: BOTH surfaces render the SAME four-step rail in order
 *   (Member identity, KYC details, Membership & consent, Documents &
 *   review); reorder/rename the code-owned list and both legs fail;
 * - FM2: the core step's wire shape is byte-identical to the standing
 *   quick-registration contract (POST /members with type/name/phone/
 *   email only) — presenting it as a wizard step changed NOTHING;
 * - FM3: abandoning after the core step is HONEST — the member exists,
 *   the wizard simply closes, and zero KYC writes happened (side-effect
 *   call count, not return values).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import { MembersScreen } from "../components/MembersScreen";
import { REGISTRATION_STEPS } from "../components/StepRail";

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

const CREATED = {
  id: "9b2f6c1e-0000-4000-8000-000000000002",
  member_no: "GP-0002",
  type: "person",
  name: "Cynthia Wairimu",
  phone: null,
  email: null,
  status: "active",
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

function res(status: number): Response {
  return { status } as Response;
}

function ok(data: unknown) {
  return { data, error: undefined, response: res(200) };
}

function wireScreen() {
  mockGet.mockImplementation((path: string) => {
    if (path === "/me/permissions") return Promise.resolve(ok(FULL_PERMISSIONS));
    if (path === "/members") return Promise.resolve(ok({ items: [], next_cursor: null }));
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

function renderScreen() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MembersScreen />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// Hand-computed oracle: the four design-flow steps, in order.
const EXPECTED_STEPS = [
  "Member identity",
  "KYC details",
  "Membership & consent",
  "Documents & review",
];

test("the code-owned step list IS the design flow's four steps in order (FM1)", () => {
  expect([...REGISTRATION_STEPS]).toEqual(EXPECTED_STEPS);
});

test("core drawer renders the rail at step 1; the core POST is byte-identical; closing the wizard after create is honest (FM1+FM2+FM3)", async () => {
  wireScreen();
  mockPost.mockResolvedValue({ data: CREATED, error: undefined, response: res(201) });
  const user = userEvent.setup();
  renderScreen();

  await user.click(await screen.findByRole("button", { name: "Register member" }));
  const dialog = await screen.findByRole("dialog", { name: "New member registration" });

  // FM1 — the rail lists all four steps with the identity step current.
  const rail = within(dialog).getByRole("list", { name: "Registration steps" });
  const items = within(rail).getAllByRole("listitem");
  expect(items.map((item) => item.textContent)).toEqual([
    "1. Member identity",
    "2. KYC details",
    "3. Membership & consent",
    "4. Documents & review",
  ]);
  expect(items[0]).toHaveAttribute("aria-current", "step");

  // FM2 — the core wire shape is the standing quick-registration
  // contract, untouched by the wizard presentation.
  await user.type(within(dialog).getByLabelText("Full name"), "Cynthia Wairimu");
  await user.click(within(dialog).getByRole("button", { name: "Register member" }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  const [path, options] = mockPost.mock.calls[0] as [string, { body: unknown }];
  expect(path).toBe("/members");
  expect(options.body).toEqual({
    type: "person",
    name: "Cynthia Wairimu",
    phone: null,
    email: null,
  });

  // The flow continues into the SAME rail, identity step completed.
  const wizard = await screen.findByRole("dialog", {
    name: `New member registration — ${CREATED.name}`,
  });
  const wizardRail = within(wizard).getByRole("list", { name: "Registration steps" });
  const wizardItems = within(wizardRail).getAllByRole("listitem");
  expect(wizardItems.map((item) => item.textContent)).toEqual([
    "✓ Member identity",
    "2. KYC details",
    "3. Membership & consent",
    "4. Documents & review",
  ]);
  expect(wizardItems[1]).toHaveAttribute("aria-current", "step");

  // FM3 — abandoning here is honest: the wizard closes, the member was
  // created by the core step, and ZERO KYC writes happened (the one
  // POST above is the member create; no kyc-profile call exists).
  await user.click(within(wizard).getByRole("button", { name: "Close" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("dialog", { name: `New member registration — ${CREATED.name}` }),
    ).not.toBeInTheDocument(),
  );
  expect(mockPost).toHaveBeenCalledTimes(1);
  const kycCalls = mockPost.mock.calls.filter(([p]) => String(p).includes("kyc"));
  expect(kycCalls).toHaveLength(0);
});
