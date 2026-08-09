/**
 * MakerCheckerPanel proofs (P15 Phase B — maker-checker SoD, UX side).
 * Falsifiable: mount checkerActions for the maker, default-allow an
 * unknown checker, or route the maker label through a parser sink and
 * the matching test fails.
 */
import { render, screen } from "@testing-library/react";
import { clearSession, setSession } from "@/modules/auth/session";
import { MakerCheckerPanel } from "../components/MakerCheckerPanel";

const MAKER_ID = "55555555-5555-5555-5555-555555555555";
const CHECKER_ID = "99999999-9999-9999-9999-999999999999";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function mountPanel() {
  return render(
    <MakerCheckerPanel
      makerId={MAKER_ID}
      makerLabel="<img src=x onerror=alert(1)> Jane Maker"
      makerAt="2 Aug 2026, 10:15"
      checkerActions={<button type="button">Approve</button>}
    >
      <div>Loan application GP-L-0042</div>
    </MakerCheckerPanel>,
  );
}

afterEach(() => {
  clearSession();
});

test("a DIFFERENT known checker sees the approval affordances in the checker context", () => {
  setSession({ accessToken: fakeJwt(CHECKER_ID), refreshToken: "r" });
  mountPanel();
  const checker = screen.getByRole("region", { name: "Checker" });
  expect(checker).toContainElement(screen.getByRole("button", { name: "Approve" }));
  // The maker context never contains the approve control.
  const maker = screen.getByRole("region", { name: "Maker" });
  expect(maker.querySelector("button")).toBeNull();
});

test("the MAKER can never approve: controls are NOT MOUNTED, SoD banner shown", () => {
  setSession({ accessToken: fakeJwt(MAKER_ID), refreshToken: "r" });
  mountPanel();
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  expect(screen.getByText(/requires a DIFFERENT/)).toBeInTheDocument();
});

test("an UNKNOWN checker identity is denied by default — no controls", () => {
  // No session at all => getOwnUserId() is null.
  mountPanel();
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  expect(screen.getByText(/withheld \(deny by default\)/)).toBeInTheDocument();
});

test("a hostile maker label renders as inert text (no sink)", () => {
  setSession({ accessToken: fakeJwt(CHECKER_ID), refreshToken: "r" });
  const { container } = mountPanel();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText(/Jane Maker/)).toBeInTheDocument();
});
