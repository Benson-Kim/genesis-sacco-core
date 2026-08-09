/**
 * #35 item 11 — dev-mode OTP display, falsifiable BOTH WAYS through the
 * REAL LoginGate (REMOVAL NOTE: this affordance and suite go away
 * before staging):
 * - when the server returned a dev code (its fail-closed flag is ON),
 *   the tester note renders with the code;
 * - when it did not (flag OFF — the default), the note is ABSENT from
 *   the DOM — not hidden, not empty: absent.
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

function mountGate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LoginGate />
    </QueryClientProvider>,
  );
}

async function requestStage(user: ReturnType<typeof userEvent.setup>) {
  mountGate();
  await user.type(screen.getByLabelText("Email or phone"), "tester@sacco.co.ke");
  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  await screen.findByLabelText("Digit 1");
}

beforeEach(() => {
  jest.clearAllMocks();
});

test("flag ON at the server (dev code returned): the tester note renders with the code", async () => {
  const user = userEvent.setup();
  mocked.requestOtp.mockResolvedValue({ devOtp: "428019" });
  await requestStage(user);

  const note = screen.getByTestId("dev-otp-display");
  expect(note).toHaveTextContent("Dev mode — OTP for testers:");
  expect(note).toHaveTextContent("428019");
});

test("flag OFF (the fail-closed default — no dev code returned): the note is ABSENT from the DOM", async () => {
  const user = userEvent.setup();
  mocked.requestOtp.mockResolvedValue({ devOtp: null });
  await requestStage(user);

  expect(screen.queryByTestId("dev-otp-display")).toBeNull();
  expect(screen.queryByText(/OTP for testers/)).toBeNull();
});
