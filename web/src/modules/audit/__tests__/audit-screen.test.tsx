/**
 * Audit viewer adversarial tests through the real screen code (salvaged
 * from duo/feature/p13-5-frontend-followthrough @ 198a238; originally
 * ported from !25 web/tests/audit.test.mjs): hostile payloads stay inert JSON
 * text, redacted entries disclose nothing and are never reconstructed,
 * pagination is keyset-only with opaque cursors, and the actor filter is
 * UUID-gated before any request leaves the client.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Providers } from "@/app/providers";
import { AuditScreen } from "../components/AuditScreen";
import * as auditApi from "../api";
import { auditEntrySchema, type AuditEntry } from "../schemas";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return { ...actual, fetchAuditPage: jest.fn() };
});

const mocked = jest.mocked(auditApi);

const HOSTILE = "<img src=x onerror=alert(1)> — <script>window.__pwned=1</script>";
const ACTOR = "fbbc9b6e-5178-4a55-8dad-913f56a93e27";

function entry(overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id: 42,
    at: "2026-07-30T12:00:00+00:00",
    actor_id: ACTOR,
    action: "user.profile",
    entity: "users",
    entity_id: "55555555-5555-5555-5555-555555555555",
    before: { full_name: HOSTILE, branch: "HQ" },
    after: { full_name: "Renamed", branch: "HQ" },
    redacted: false,
    ...overrides,
  };
}

function mountScreen() {
  return render(
    <Providers>
      <AuditScreen />
    </Providers>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.fetchAuditPage.mockResolvedValue({ items: [entry()], nextCursor: null });
});

test("hostile audit payload renders as inert pretty-printed JSON text — the named threat", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  await user.click(await screen.findByText("users"));
  const drawer = await screen.findByRole("dialog", { name: "Audit entry" });

  // Byte-identical JSON text exactly as the API returned it…
  const expected = JSON.stringify({ full_name: HOSTILE, branch: "HQ" }, null, 2);
  const preNodes = drawer.querySelectorAll("pre");
  expect(preNodes.length).toBe(2);
  expect(preNodes[0]?.textContent).toBe(expected);
  // …never parsed into live elements (falsifiable: render the payload via
  // a parser sink and these appear).
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
});

test("redacted entries disclose no payload and no client-side reconstruction", async () => {
  const user = userEvent.setup();
  mocked.fetchAuditPage.mockResolvedValue({
    items: [entry({ before: null, after: null, redacted: true })],
    nextCursor: null,
  });
  const { container } = mountScreen();

  expect(await screen.findByText("Redacted")).toBeInTheDocument();
  await user.click(screen.getByText("users"));
  const drawer = await screen.findByRole("dialog", { name: "Audit entry" });

  expect(within(drawer).getByText(/Payload redacted/)).toBeInTheDocument();
  expect(within(drawer).getAllByText("Redacted per role entitlement.").length).toBe(2);
  // No payload block exists and nothing resembling the redacted fields is
  // synthesized anywhere (no-reconstruction proof: the hostile marker of
  // the unredacted fixture must be absent).
  expect(container.querySelector("pre")).toBeNull();
  expect(container.textContent).not.toContain(HOSTILE);
  expect(container.textContent).not.toContain("full_name");
});

test("pagination is keyset-only: Load more echoes the opaque cursor untouched", async () => {
  const user = userEvent.setup();
  const cursor = "b3BhcXVlLWN1cnNvcg==:not-your-business";
  mocked.fetchAuditPage
    .mockResolvedValueOnce({ items: [entry()], nextCursor: cursor })
    .mockResolvedValueOnce({ items: [entry({ id: 41 })], nextCursor: null });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));

  await waitFor(() => expect(mocked.fetchAuditPage).toHaveBeenCalledTimes(2));
  // First page: null cursor; second page: the server cursor VERBATIM.
  expect(mocked.fetchAuditPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchAuditPage.mock.calls[1]?.[1]).toBe(cursor);
});

test("actor filter must be a UUID before any request leaves the client", async () => {
  const user = userEvent.setup();
  mountScreen();
  await screen.findByText("users");
  expect(mocked.fetchAuditPage).toHaveBeenCalledTimes(1);

  await user.type(screen.getByPlaceholderText("actor UUID"), "1 OR 1=1; --");
  await user.click(screen.getByRole("button", { name: "Apply filters" }));

  expect(await screen.findByText("Actor filter must be a UUID.")).toBeInTheDocument();
  // No new request was issued with the hostile filter.
  expect(mocked.fetchAuditPage).toHaveBeenCalledTimes(1);

  // A valid UUID goes through as a bound filter value.
  await user.clear(screen.getByPlaceholderText("actor UUID"));
  await user.type(screen.getByPlaceholderText("actor UUID"), ACTOR);
  await user.click(screen.getByRole("button", { name: "Apply filters" }));
  await waitFor(() => expect(mocked.fetchAuditPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchAuditPage.mock.calls[1]?.[0]).toMatchObject({ actorId: ACTOR });
});

test("issue #30 A2/S2 accept/reject matrix: `at` asserts the datetime.isoformat() shape (api/audit_log.py) — fmtDateTime can never render 'Invalid Date'", () => {
  const withAt = (at: string) => auditEntrySchema.safeParse(entry({ at })).success;

  // Legitimate serialisations ACCEPTED (hand-computed from
  // datetime.isoformat(): naive, fractional-seconds, offset, Zulu).
  expect(withAt("2026-07-30T12:00:00")).toBe(true);
  expect(withAt("2026-07-30T12:00:00.123456")).toBe(true);
  expect(withAt("2026-07-30T12:00:00+00:00")).toBe(true);
  expect(withAt("2026-07-30T12:00:00Z")).toBe(true);

  // Garbage REJECTED — each previously flowed into fmtDateTime
  // unchallenged (bare z.string()) on the register AND the drawer.
  for (const at of [
    "yesterday-ish",
    "2026-07-30",
    "2026-07-30 12:00:00",
    "30/07/2026T12:00:00",
    "2026-07-30T12:00",
    "",
  ]) {
    expect(withAt(at)).toBe(false);
  }
});
