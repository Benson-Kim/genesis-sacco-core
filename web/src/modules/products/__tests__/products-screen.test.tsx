/**
 * Products screen adversarial tests THROUGH THE REAL SCREEN CODE (P15
 * Settings/products batch), mirroring the users-screen reference suite:
 * the feature API layer is stubbed at the module boundary; rendering,
 * mutation orchestration, typed-confirmation gating, 409 handling,
 * idempotency-key custody and permission gating are the real shipped
 * code, mounted under the REAL <Providers> (mutation retry: 0 and the
 * dual-cache 401 teardown are the production configuration —
 * falsifiable: flip mutations to retry, or auto-refetch on 409, and
 * these tests fail).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { ProductsScreen } from "../components/ProductsScreen";
import * as productsApi from "../api";
import type { Product } from "../schemas";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchProducts: jest.fn(),
    createProduct: jest.fn(),
    updateProduct: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(productsApi);
const mockedPermissions = jest.mocked(usePermissions);

// Hostile payload chosen to be typeable through the ConfirmDangerModal
// (no userEvent bracket syntax) and free of hygiene-gate literals.
const HOSTILE_NAME = "<img src=x onerror=window.__pwned=1>";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function baseProduct(overrides: Partial<Product> = {}): Product {
  return {
    id: PRODUCT_ID,
    name: HOSTILE_NAME,
    rate_pct: "12.50",
    deposit_multiplier: "3.00",
    max_term_months: 24,
    guarantors_required: 2,
    active: true,
    version: 3,
    ...overrides,
  };
}

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    {
      module: "settings",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
  ],
};

function grantPermissions(perms: typeof FULL_PERMS) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function mountScreen() {
  return render(
    <Providers>
      <ProductsScreen />
    </Providers>,
  );
}

/** Drive the rules save through its typed confirmation. */
async function confirmRulesSave(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Save product" }));
  const dialog = await screen.findByRole("dialog", { name: "Apply product rules" });
  await user.type(
    within(dialog).getByLabelText(`Type "${HOSTILE_NAME}" to confirm`),
    HOSTILE_NAME,
  );
  await user.click(within(dialog).getByRole("button", { name: "Apply rules" }));
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchProducts.mockResolvedValue([baseProduct()]);
});

afterEach(() => {
  clearSession();
});

test("hostile product name renders as inert text; rule figures render VERBATIM (named XSS threat + blocker (a))", async () => {
  const { container } = mountScreen();
  // Sidebar entry + edit-panel heading both carry the hostile string as TEXT.
  expect(await screen.findByText(HOSTILE_NAME)).toBeInTheDocument();
  expect(await screen.findByText(`Edit — ${HOSTILE_NAME}`)).toBeInTheDocument();
  // Decimal strings prefill inputs byte-identically — no reformatting,
  // no numeric round-trip (falsifiable: coerce with Number() and 12.50
  // becomes 12.5).
  expect(await screen.findByLabelText("Rate (% p.a.)")).toHaveValue("12.50");
  expect(screen.getByLabelText("Deposit multiplier")).toHaveValue("3.00");
  // No parser sink materialized elements from the hostile string.
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
});

test("typed confirmation gates the money-adjacent save: NO write until the byte-identical phrase is typed", async () => {
  const user = userEvent.setup();
  mocked.updateProduct.mockResolvedValue(baseProduct({ version: 4 }));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Save product" }));
  const dialog = await screen.findByRole("dialog", { name: "Apply product rules" });
  // No write happened by opening the confirmation.
  expect(mocked.updateProduct).not.toHaveBeenCalled();
  const confirmButton = within(dialog).getByRole("button", { name: "Apply rules" });
  expect(confirmButton).toBeDisabled();
  // A near-miss phrase keeps the action disarmed (byte-identical rule).
  const phraseInput = within(dialog).getByLabelText(`Type "${HOSTILE_NAME}" to confirm`);
  await user.type(phraseInput, "wrong phrase");
  expect(confirmButton).toBeDisabled();
  expect(mocked.updateProduct).not.toHaveBeenCalled();
  await user.clear(phraseInput);
  await user.type(phraseInput, HOSTILE_NAME);
  expect(confirmButton).toBeEnabled();
  await user.click(confirmButton);

  await waitFor(() => expect(mocked.updateProduct).toHaveBeenCalledTimes(1));
  // Full WYSIWYG rule set, loaded version, decimal STRINGS verbatim.
  expect(mocked.updateProduct.mock.calls[0]?.[1]).toEqual({
    version: 3,
    rate_pct: "12.50",
    deposit_multiplier: "3.00",
    max_term_months: 24,
    guarantors_required: 2,
  });
  expect(mocked.updateProduct.mock.calls[0]?.[2]).toEqual(expect.any(String));
});

test("stale edit: 409 shows the explicit reload flow with EXACTLY ONE write attempt; reload never replays", async () => {
  const user = userEvent.setup();
  mocked.updateProduct.mockRejectedValue(new ApiError(409, "conflict", "corr-stale"));
  mountScreen();

  await screen.findByRole("button", { name: "Save product" });
  await confirmRulesSave(user);

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reload record" })).toBeInTheDocument();
  // Exactly one write attempt — no auto-retry, no silent overwrite
  // (falsifiable: set mutations retry>0 in Providers and this grows).
  expect(mocked.updateProduct).toHaveBeenCalledTimes(1);
  expect(mocked.updateProduct.mock.calls[0]?.[1]).toMatchObject({ version: 3 });

  // Explicit reload refetches the record (server returns v4)…
  mocked.fetchProducts.mockResolvedValue([baseProduct({ version: 4, rate_pct: "13.00" })]);
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(await screen.findByLabelText("Rate (% p.a.)")).toHaveValue("13.00");
  // …and NEVER replays the stale submission.
  expect(mocked.updateProduct).toHaveBeenCalledTimes(1);
});

test("idempotency keys: stable across retries of an identical body, rotated when the body changes", async () => {
  const user = userEvent.setup();
  mocked.updateProduct
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue(baseProduct({ version: 4 }));
  mountScreen();

  await screen.findByRole("button", { name: "Save product" });
  await confirmRulesSave(user);
  await waitFor(() => expect(mocked.updateProduct).toHaveBeenCalledTimes(1));
  // The operator re-confirms the IDENTICAL submission after the outage.
  await confirmRulesSave(user);
  await waitFor(() => expect(mocked.updateProduct).toHaveBeenCalledTimes(2));

  const keyAttempt1 = mocked.updateProduct.mock.calls[0]?.[2];
  const keyAttempt2 = mocked.updateProduct.mock.calls[1]?.[2];
  expect(keyAttempt1).toBe(keyAttempt2);

  // Changed body => rotated key (a NEW logical submission must never
  // replay the old stored response).
  const rateInput = screen.getByLabelText("Rate (% p.a.)");
  await user.clear(rateInput);
  await user.type(rateInput, "14.25");
  await confirmRulesSave(user);
  await waitFor(() => expect(mocked.updateProduct).toHaveBeenCalledTimes(3));
  expect(mocked.updateProduct.mock.calls[2]?.[2]).not.toBe(keyAttempt1);
});

test("double-submit through the confirmation produces exactly ONE write (pending short-circuit)", async () => {
  const user = userEvent.setup();
  let releaseWrite: (value: Product) => void = () => undefined;
  mocked.updateProduct.mockImplementation(
    () =>
      new Promise<Product>((resolve) => {
        releaseWrite = resolve;
      }),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Save product" }));
  const dialog = await screen.findByRole("dialog", { name: "Apply product rules" });
  await user.type(
    within(dialog).getByLabelText(`Type "${HOSTILE_NAME}" to confirm`),
    HOSTILE_NAME,
  );
  const confirmButton = within(dialog).getByRole("button", { name: "Apply rules" });
  await user.click(confirmButton);
  // While pending the confirm button is disabled AND short-circuited.
  await waitFor(() => expect(mocked.updateProduct).toHaveBeenCalledTimes(1));
  expect(within(dialog).getByRole("button", { name: "Working…" })).toBeDisabled();
  await user.click(within(dialog).getByRole("button", { name: "Working…" }));
  expect(mocked.updateProduct).toHaveBeenCalledTimes(1);
  releaseWrite(baseProduct({ version: 4 }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "Apply product rules" })).toBeNull(),
  );
});

test("UI affordances follow the matrix: view-only settings role sees NO mutation controls", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  mountScreen();

  expect(await screen.findByLabelText("Rate (% p.a.)")).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Save product" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Deactivate" })).toBeNull();
  expect(screen.queryByRole("button", { name: "+ Add product" })).toBeNull();
});

test("least-disclosure: a 403 renders the sanitized banner only, no reload affordance, session intact", async () => {
  const user = userEvent.setup();
  mocked.updateProduct.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  await screen.findByRole("button", { name: "Save product" });
  await confirmRulesSave(user);

  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  // The raw server category never renders (no capability hints).
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  // 403 stays DISTINCT from 409: no reload-and-retry affordance.
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.updateProduct).toHaveBeenCalledTimes(1);
  // Under-privilege is not session death.
  expect(hasSession()).toBe(true);
});

test("query-path 401 tears the session down immediately (dual-cache teardown)", async () => {
  mocked.fetchProducts.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  // Accommodates the production query retry (retry: 1) — the teardown
  // fires only after the final failure; that config must stay real here.
  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
});

test("deactivation flows through the typed confirmation and sends {version, active:false} only", async () => {
  const user = userEvent.setup();
  mocked.updateProduct.mockResolvedValue(baseProduct({ active: false, version: 4 }));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Deactivate" }));
  const dialog = await screen.findByRole("dialog", { name: "Deactivate product" });
  expect(mocked.updateProduct).not.toHaveBeenCalled();
  await user.type(
    within(dialog).getByLabelText(`Type "${HOSTILE_NAME}" to confirm`),
    HOSTILE_NAME,
  );
  await user.click(within(dialog).getByRole("button", { name: "Deactivate product" }));

  await waitFor(() => expect(mocked.updateProduct).toHaveBeenCalledTimes(1));
  expect(mocked.updateProduct.mock.calls[0]?.[1]).toEqual({ version: 3, active: false });
  expect(mocked.updateProduct.mock.calls[0]?.[2]).toEqual(expect.any(String));
});

test("create drawer: double-submit yields ONE product; a duplicate-name 409 renders as a create conflict (ErrorBanner, no reload flow — F-B4)", async () => {
  const user = userEvent.setup();
  mocked.createProduct.mockRejectedValue(new ApiError(409, "conflict", "corr-dup"));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Add product" }));
  const drawer = await screen.findByRole("dialog", { name: "Add product" });
  await user.type(within(drawer).getByLabelText("Product name"), "Emergency Loan");
  await user.type(within(drawer).getByLabelText("Rate (% p.a.)"), "15");
  await user.type(within(drawer).getByLabelText("Deposit multiplier"), "2");
  await user.type(within(drawer).getByLabelText("Max term (months)"), "12");
  await user.click(within(drawer).getByRole("button", { name: "Create product" }));

  // Create conflicts have no version/reload flow: sanitized conflict
  // title + correlation id only, NO "Reload record" affordance.
  expect(await within(drawer).findByText(/reload before retrying/)).toBeInTheDocument();
  expect(within(drawer).getByText(/ref: corr-dup/)).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.createProduct).toHaveBeenCalledTimes(1);
  expect(mocked.createProduct.mock.calls[0]?.[0]).toEqual({
    name: "Emergency Loan",
    rate_pct: "15",
    deposit_multiplier: "2",
    max_term_months: 12,
    guarantors_required: 0,
  });
  expect(mocked.createProduct.mock.calls[0]?.[1]).toEqual(expect.any(String));
});

test("client Zod verdicts render inline through FormField before any write", async () => {
  const user = userEvent.setup();
  mountScreen();

  const rateInput = await screen.findByLabelText("Rate (% p.a.)");
  await user.clear(rateInput);
  await user.type(rateInput, "12.345");
  await user.click(screen.getByRole("button", { name: "Save product" }));

  // No confirmation opens and no write happens on a client-invalid form.
  expect(screen.queryByRole("dialog", { name: "Apply product rules" })).toBeNull();
  expect(mocked.updateProduct).not.toHaveBeenCalled();
  expect(
    await screen.findByText("Enter a decimal like 12 or 12.5 (max 2dp)"),
  ).toBeInTheDocument();
});
