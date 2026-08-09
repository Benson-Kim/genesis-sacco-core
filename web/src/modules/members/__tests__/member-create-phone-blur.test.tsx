/**
 * #35 item 1 — blur-time Kenya-phone validation on the register-member
 * drawer, through the REAL drawer code (API stubbed at the module
 * boundary). Falsifiable legs:
 * - an invalid phone SHOWS the message on blur and blocks submit with
 *   ZERO wire calls (the server would 422 it — the drawer mirrors);
 * - correcting the value CLEARS the message while typing;
 * - a valid phone blurs clean and submits exactly once;
 * - an empty phone is fine (the field is optional).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemberCreateDrawer } from "../components/MemberCreateDrawer";
import * as membersApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return { ...actual, createMember: jest.fn() };
});

const mocked = jest.mocked(membersApi);

const BLUR_MESSAGE =
  "Enter a Kenya mobile number like 0712345678, 0110000000 or +254712345678.";

function mountDrawer() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemberCreateDrawer onClose={jest.fn()} onCreated={jest.fn()} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
});

test("invalid phone: the message SHOWS on blur, submit is blocked with ZERO wire calls, and correcting the value CLEARS it", async () => {
  const user = userEvent.setup();
  mountDrawer();

  await user.type(screen.getByLabelText("Full name"), "Blur Test");
  const phone = screen.getByLabelText("Phone (optional)");
  await user.type(phone, "0812345678"); // 08 — not a Kenya mobile prefix
  await user.tab(); // blur

  expect(await screen.findByText(BLUR_MESSAGE)).toBeInTheDocument();

  // Submit while invalid: refused locally, nothing reaches the wire.
  await user.click(screen.getByRole("button", { name: "Register member" }));
  expect(mocked.createMember).not.toHaveBeenCalled();
  expect(screen.getByText(BLUR_MESSAGE)).toBeInTheDocument();

  // Correction clears the message WHILE TYPING (no second blur needed).
  await user.clear(phone);
  await user.type(phone, "0712345678");
  expect(screen.queryByText(BLUR_MESSAGE)).toBeNull();
});

test("valid phone blurs clean and submits exactly once; empty phone stays optional", async () => {
  const user = userEvent.setup();
  let release: (value: never) => void = () => undefined;
  mocked.createMember.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = resolve as (value: never) => void;
      }),
  );
  mountDrawer();

  await user.type(screen.getByLabelText("Full name"), "Valid Phone");
  const phone = screen.getByLabelText("Phone (optional)");
  await user.type(phone, "+254712345678");
  await user.tab();
  expect(screen.queryByText(BLUR_MESSAGE)).toBeNull();

  await user.click(screen.getByRole("button", { name: "Register member" }));
  expect(mocked.createMember).toHaveBeenCalledTimes(1);
  expect(mocked.createMember.mock.calls[0]?.[0]).toMatchObject({
    name: "Valid Phone",
    phone: "+254712345678",
  });
  release(undefined as never);

  // Empty phone never warns — the field is optional.
  mountDrawer();
  const phones = screen.getAllByLabelText("Phone (optional)");
  await user.click(phones[phones.length - 1]!);
  await user.tab();
  expect(screen.queryByText(BLUR_MESSAGE)).toBeNull();
});
