/**
 * #35 item 1 — sign-in identifier (email) blur validation through the
 * REAL LoginGate. Falsifiable legs:
 * - an invalid identifier SHOWS the message on blur (no wire call);
 * - correcting the value CLEARS it while typing;
 * - a valid email blurs clean and the request flow still works.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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

const BLUR_MESSAGE = "Enter your registered email address.";

function mountGate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LoginGate />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
});

test("invalid identifier: message SHOWS on blur with zero wire calls; correction CLEARS it while typing", async () => {
  const user = userEvent.setup();
  mountGate();

  const email = screen.getByLabelText("Email or phone");
  await user.type(email, "not-an-email");
  await user.tab(); // blur

  expect(await screen.findByText(BLUR_MESSAGE)).toBeInTheDocument();
  expect(mocked.requestOtp).not.toHaveBeenCalled();

  // Submitting while invalid is refused locally — still zero wire calls.
  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  expect(mocked.requestOtp).not.toHaveBeenCalled();

  // Correction clears the message as the user types.
  await user.clear(email);
  await user.type(email, "teller@sacco.co.ke");
  expect(screen.queryByText(BLUR_MESSAGE)).toBeNull();
});

test("valid email blurs clean and requests the OTP exactly once", async () => {
  const user = userEvent.setup();
  mocked.requestOtp.mockResolvedValue({ devOtp: null });
  mountGate();

  const email = screen.getByLabelText("Email or phone");
  await user.type(email, "teller@sacco.co.ke");
  await user.tab();
  expect(screen.queryByText(BLUR_MESSAGE)).toBeNull();

  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  await screen.findByLabelText("Digit 1");
  expect(mocked.requestOtp).toHaveBeenCalledTimes(1);
  // mutationFn receives (variables, context) — assert the variable only.
  expect(mocked.requestOtp.mock.calls[0]?.[0]).toBe("teller@sacco.co.ke");
});
