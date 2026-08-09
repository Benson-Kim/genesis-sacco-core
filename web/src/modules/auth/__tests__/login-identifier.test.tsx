/**
 * #35 sign-in identifier — live as-you-type classification (email vs
 * phone) through the REAL LoginGate + the msisdn mirror. Falsifiable
 * legs (oracles hand-computed, byte-identical to the backend suite):
 * - typing '07…' classifies PHONE (inputMode flips to tel) and
 *   validates under the msisdn rule; 'user@…' classifies EMAIL;
 * - an invalid phone shows the phone message via the shared FormField
 *   wiring (role=alert, aria-describedby) and submit stays LOCAL —
 *   zero wire calls;
 * - a valid phone submit carries the NORMALIZED +2547… identifier on
 *   the wire (hand oracle: 0712345678 -> '+254' + '7' + '12345678');
 * - the verify submit carries the same normalized identifier;
 * - an unknown identifier gets the server's non-disclosing 202 and the
 *   standard verify stage renders (no existence hint).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LoginGate } from "../components/LoginGate";
import { KENYA_PHONE_MESSAGE, normalizeKenyaMsisdn } from "@/lib/phone";
import { classifyIdentifier } from "../schemas";
import * as authApi from "../api";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: jest.fn() }),
}));

jest.mock("../api", () => ({
  requestOtp: jest.fn(),
  verifyOtp: jest.fn(),
}));

const mocked = jest.mocked(authApi);

const PHONE_MESSAGE = KENYA_PHONE_MESSAGE;
const EMAIL_MESSAGE = "Enter your registered email address.";

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

describe("msisdn mirror (oracles byte-identical to the backend suite)", () => {
  test("the four accepted spellings normalize to their E.164 form", () => {
    // Hand-computed: 0(7)(12345678) -> +254 + 7 + 12345678.
    expect(normalizeKenyaMsisdn("0712345678")).toBe("+254712345678");
    expect(normalizeKenyaMsisdn("0110000000")).toBe("+254110000000");
    expect(normalizeKenyaMsisdn("+254712345678")).toBe("+254712345678");
    expect(normalizeKenyaMsisdn(" 0712345678 ")).toBe("+254712345678");
  });

  test("rejections mirror the backend: no digit harvesting, no other prefixes", () => {
    expect(normalizeKenyaMsisdn("0712")).toBeNull();
    expect(normalizeKenyaMsisdn("0812345678")).toBeNull();
    expect(normalizeKenyaMsisdn("07123456789")).toBeNull();
    expect(normalizeKenyaMsisdn("call 0712345678")).toBeNull();
  });

  test("classification: digit-runs type as phone, everything else as email", () => {
    expect(classifyIdentifier("07")).toBe("phone");
    expect(classifyIdentifier("+2547")).toBe("phone");
    expect(classifyIdentifier("user@sacco.co.ke")).toBe("email");
    expect(classifyIdentifier("user")).toBe("email");
    expect(classifyIdentifier("")).toBe("email");
  });
});

test("typing '07…' flips the field to phone mode; '@' flips it back to email", async () => {
  const user = userEvent.setup();
  mountGate();

  const field = screen.getByLabelText("Email or phone");
  expect(field).toHaveAttribute("inputmode", "email");

  await user.type(field, "07");
  expect(field).toHaveAttribute("inputmode", "tel");

  await user.clear(field);
  await user.type(field, "user@");
  expect(field).toHaveAttribute("inputmode", "email");
});

test("invalid phone: message shows on blur through FormField wiring; submit stays local (ZERO wire calls); correction clears while typing", async () => {
  const user = userEvent.setup();
  mountGate();

  const field = screen.getByLabelText("Email or phone");
  await user.type(field, "0712");
  await user.tab(); // blur

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(PHONE_MESSAGE);
  // Shared FormField wiring, not a local error div.
  expect(field).toHaveAttribute("aria-invalid", "true");
  expect(field).toHaveAttribute("aria-describedby", "login-identifier-error");

  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  expect(mocked.requestOtp).not.toHaveBeenCalled();

  // Completing the number clears the message while typing.
  await user.type(field, "345678");
  expect(screen.queryByText(PHONE_MESSAGE)).toBeNull();
  expect(mocked.requestOtp).not.toHaveBeenCalled();
});

test("invalid email keeps the email rule and message; zero wire calls", async () => {
  const user = userEvent.setup();
  mountGate();

  const field = screen.getByLabelText("Email or phone");
  await user.type(field, "not-an-email");
  await user.tab();

  expect(await screen.findByText(EMAIL_MESSAGE)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  expect(mocked.requestOtp).not.toHaveBeenCalled();
});

test("valid phone submit carries the NORMALIZED identifier; verify carries the same", async () => {
  const user = userEvent.setup();
  mocked.requestOtp.mockResolvedValue({ devOtp: null });
  let release: (value: never) => void = () => undefined;
  mocked.verifyOtp.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = resolve as (value: never) => void;
      }),
  );
  mountGate();

  await user.type(screen.getByLabelText("Email or phone"), "0712345678");
  await user.click(screen.getByRole("button", { name: "Send OTP" }));
  await screen.findByLabelText("Digit 1");

  expect(mocked.requestOtp).toHaveBeenCalledTimes(1);
  // Hand oracle, never captured from the code: '+254712345678'.
  expect(mocked.requestOtp.mock.calls[0]?.[0]).toBe("+254712345678");

  for (let index = 1; index <= 6; index += 1) {
    await user.type(screen.getByLabelText(`Digit ${index}`), String(index));
  }
  await user.click(screen.getByRole("button", { name: "Verify & sign in" }));
  expect(mocked.verifyOtp).toHaveBeenCalledTimes(1);
  expect(mocked.verifyOtp.mock.calls[0]?.[0]).toMatchObject({
    identifier: "+254712345678",
    code: "123456",
  });
  release(undefined as never);
});

test("unknown identifier: the non-disclosing 202 renders the STANDARD verify stage", async () => {
  const user = userEvent.setup();
  // The server answers the same shape for known and unknown identifiers;
  // the gate must render the standard message either way.
  mocked.requestOtp.mockResolvedValue({ devOtp: null });
  mountGate();

  await user.type(screen.getByLabelText("Email or phone"), "0733999999");
  await user.click(screen.getByRole("button", { name: "Send OTP" }));

  expect(await screen.findByText(/Enter the 6-digit code sent to/)).toBeInTheDocument();
  // The subtitle echoes what the operator TYPED (the wire carried the
  // normalized form — asserted in the valid-phone leg above).
  expect(screen.getByText("0733999999")).toBeInTheDocument();
});
