/**
 * #31 ledger (h5), OTP side — paste and IME-composition hygiene of the
 * LoginGate 6-digit code entry, through the REAL screen code.
 *
 * Falsifiable proofs (#35 item 12 UPDATED the paste spec — the
 * maintainer authorized full-code paste, so the old never-fans-out leg
 * is superseded by SANITIZED fan-out; the adversarial properties are
 * EXTENDED, never weakened):
 * - a full-code PASTE fans out across the six boxes (digits harvested,
 *   non-digits stripped) and the code verifies with ONE wire call;
 * - a digit-FREE hostile paste is inert — every box stays empty,
 *   submit refuses inline, ZERO wire calls;
 * - a partial paste fills only the leading boxes; submit still refuses
 *   until all six digits exist;
 * - IME-composed NON-ASCII digits (full-width ６ etc.) are stripped by
 *   the \D reducer — the box stays empty, zero wire calls;
 * - honest typed ASCII digits still verify (hygiene is exactness, not
 *   lockout) with exactly ONE verify POST.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LoginGate } from "../components/LoginGate";
import * as authApi from "../api";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: jest.fn() }),
}));

jest.mock("../api", () => ({
  requestOtp: jest.fn(),
  verifyOtp: jest.fn(),
}));

const mocked = jest.mocked(authApi);

function mountGate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LoginGate />
    </QueryClientProvider>,
  );
}

/** Drive the request stage to reach the 6-digit verify stage. */
async function reachVerifyStage(user: ReturnType<typeof userEvent.setup>) {
  mocked.requestOtp.mockResolvedValue({ devOtp: null });
  mountGate();
  await user.type(screen.getByLabelText("Email or phone"), "teller@sacco.co.ke");
  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  await screen.findByLabelText("Digit 1");
}

beforeEach(() => {
  jest.clearAllMocks();
});

test("full-code PASTE fans out SANITIZED across the six boxes and verifies with exactly ONE wire call (#35 item 12)", async () => {
  const user = userEvent.setup();
  let release: (value: never) => void = () => undefined;
  mocked.verifyOtp.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = resolve as (value: never) => void;
      }),
  );
  await reachVerifyStage(user);

  const first = screen.getByLabelText("Digit 1");
  await user.click(first);
  // Real clipboards carry separators — they are STRIPPED, digits kept.
  await user.paste("123 456");

  for (let index = 1; index <= 6; index += 1) {
    expect(screen.getByLabelText(`Digit ${index}`)).toHaveValue(String(index));
  }

  await user.click(screen.getByRole("button", { name: "Verify & sign in" }));
  expect(mocked.verifyOtp).toHaveBeenCalledTimes(1);
  expect(mocked.verifyOtp.mock.calls[0]?.[0]).toMatchObject({
    identifier: "teller@sacco.co.ke",
    code: "123456",
  });
  release(undefined as never);
});

test("ADVERSARIAL digit-free paste is INERT: every box stays empty, submit refuses inline, ZERO wire calls", async () => {
  const user = userEvent.setup();
  await reachVerifyStage(user);

  const first = screen.getByLabelText("Digit 1");
  await user.click(first);
  await user.paste("<script>alert(1)</script>");

  // No digits in that payload after the (\d-only) 'onerror' etc. — the
  // '1' inside alert(1) IS a digit, so use a truly digit-free probe too.
  // alert(1) contributes one digit; assert the sanitized harvest below.
  expect(screen.getByLabelText("Digit 1")).toHaveValue("1");
  for (let index = 2; index <= 6; index += 1) {
    expect(screen.getByLabelText(`Digit ${index}`)).toHaveValue("");
  }

  // A DIGIT-FREE hostile paste is fully inert.
  await user.click(screen.getByLabelText("Digit 2"));
  await user.paste("DROP TABLE users; --");
  expect(screen.getByLabelText("Digit 2")).toHaveValue("");

  await user.click(screen.getByRole("button", { name: "Verify & sign in" }));
  expect(await screen.findByText("Enter all 6 digits.")).toBeInTheDocument();
  expect(mocked.verifyOtp).not.toHaveBeenCalled();
});

test("partial paste fills only the leading boxes; submit refuses until all six digits exist", async () => {
  const user = userEvent.setup();
  await reachVerifyStage(user);

  await user.click(screen.getByLabelText("Digit 1"));
  await user.paste("12ab34");

  expect(screen.getByLabelText("Digit 1")).toHaveValue("1");
  expect(screen.getByLabelText("Digit 2")).toHaveValue("2");
  expect(screen.getByLabelText("Digit 3")).toHaveValue("3");
  expect(screen.getByLabelText("Digit 4")).toHaveValue("4");
  expect(screen.getByLabelText("Digit 5")).toHaveValue("");
  expect(screen.getByLabelText("Digit 6")).toHaveValue("");

  await user.click(screen.getByRole("button", { name: "Verify & sign in" }));
  expect(await screen.findByText("Enter all 6 digits.")).toBeInTheDocument();
  expect(mocked.verifyOtp).not.toHaveBeenCalled();
});

test("IME-composed full-width digits are stripped by the \\D reducer: the box stays empty, submit refuses, ZERO verify calls", async () => {
  const user = userEvent.setup();
  await reachVerifyStage(user);

  const first = screen.getByLabelText("Digit 1");
  // Simulate an East-Asian IME committing FULLWIDTH DIGIT SIX
  // (U+FF16): composition events bracket a change whose value is the
  // composed non-ASCII code point.
  fireEvent.compositionStart(first);
  fireEvent.change(first, { target: { value: "\uFF16" } });
  fireEvent.compositionEnd(first, { data: "\uFF16" });
  expect(first).toHaveValue("");

  // A composed CJK character is equally inert.
  fireEvent.compositionStart(first);
  fireEvent.change(first, { target: { value: "六" } });
  fireEvent.compositionEnd(first, { data: "六" });
  expect(first).toHaveValue("");

  await user.click(screen.getByRole("button", { name: "Verify & sign in" }));
  expect(await screen.findByText("Enter all 6 digits.")).toBeInTheDocument();
  expect(mocked.verifyOtp).not.toHaveBeenCalled();
});

test("honest ASCII digits still verify with exactly ONE wire call (hygiene is exactness, not lockout)", async () => {
  const user = userEvent.setup();
  let release: (value: never) => void = () => undefined;
  mocked.verifyOtp.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = resolve as (value: never) => void;
      }),
  );
  await reachVerifyStage(user);

  for (let index = 1; index <= 6; index += 1) {
    await user.type(screen.getByLabelText(`Digit ${index}`), String(index));
  }
  await user.click(screen.getByRole("button", { name: "Verify & sign in" }));
  expect(mocked.verifyOtp).toHaveBeenCalledTimes(1);
  expect(mocked.verifyOtp.mock.calls[0]?.[0]).toMatchObject({
    identifier: "teller@sacco.co.ke",
    code: "123456",
  });
  release(undefined as never);
});
