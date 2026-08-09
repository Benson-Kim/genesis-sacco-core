import { render, screen } from "@testing-library/react";
import { Kv } from "../components/Kv";
import { Pill } from "../components/Pill";

// Attacker-influenced free-text (member names, refs, purposes) flows into
// Kv rows on every drawer — must render as inert text (named XSS threat;
// same hostile strings convention as the screen suites).
const HOSTILE_LABEL = '<img src=x onerror=window.__pwned=1> "quoted" label';
const HOSTILE_VALUE = "<script>window.__pwned=2</script>KES 1,234.00";

describe("Kv (label/value detail row — #31 batch 9, DA-72.2)", () => {
  it("FM1: renders the label and the value as text, label span first", () => {
    // Hand-computed oracle: exactly one row div containing exactly two
    // spans — [0] the label, [1] the value.
    const { container } = render(<Kv label="Member no">GP-0007</Kv>);
    const row = container.firstElementChild;
    expect(row).not.toBeNull();
    expect((row as HTMLElement).tagName).toBe("DIV");
    const spans = (row as HTMLElement).querySelectorAll("span");
    expect(spans).toHaveLength(2);
    expect(spans[0]).toHaveTextContent("Member no");
    expect(spans[1]).toHaveTextContent("GP-0007");
  });

  it("FM1b: value accepts structured ReactNode children (Pill), not only strings", () => {
    render(
      <Kv label="Status">
        <Pill bg="emeraldSoft" color="emerald">
          Active
        </Pill>
      </Kv>,
    );
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("FM2: variant behaviour — default row does NOT carry the quiet modifier; variant=\"quiet\" does (both directions)", () => {
    const { container } = render(
      <>
        <Kv label="Default row">1</Kv>
        <Kv label="Quiet row" variant="quiet">
          2
        </Kv>
      </>,
    );
    expect(container.children).toHaveLength(2);
    const defaultRow = screen.getByText("Default row").closest("div") as HTMLElement;
    const quietRow = screen.getByText("Quiet row").closest("div") as HTMLElement;
    expect(defaultRow.className).toContain("kvRow");
    expect(defaultRow.className).not.toContain("quiet");
    expect(quietRow.className).toContain("kvRow");
    expect(quietRow.className).toContain("quiet");
  });

  it("FM3: hostile label/value strings are inert — no element injection, no script execution", () => {
    const { container } = render(<Kv label={HOSTILE_LABEL}>{HOSTILE_VALUE}</Kv>);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(
      (window as unknown as { __pwned?: number }).__pwned,
    ).toBeUndefined();
    // The hostile payloads render verbatim as inert TEXT.
    expect(screen.getByText(HOSTILE_LABEL)).toBeInTheDocument();
    expect(screen.getByText(HOSTILE_VALUE)).toBeInTheDocument();
  });

  it("FM5: valueClassName is APPENDED to the value span's class list when given — and ABSENT from it when omitted (both directions)", () => {
    // Hand-computed oracle: the composed value span carries exactly
    // {kvVal, tnum}; the plain value span carries kvVal ONLY (the
    // batch-9 rendered class set, unchanged by the new prop).
    render(
      <>
        <Kv label="Composed" variant="quiet" valueClassName="tnum">
          12:00
        </Kv>
        <Kv label="Plain" variant="quiet">
          12:01
        </Kv>
      </>,
    );
    const composedVal = screen.getByText("12:00");
    const plainVal = screen.getByText("12:01");
    expect(composedVal.className).toContain("kvVal");
    expect(composedVal.className).toContain("tnum");
    expect(plainVal.className).toContain("kvVal");
    expect(plainVal.className).not.toContain("tnum");
  });

  it("FM6: a hostile string through valueClassName is inert — React-escaped class ATTRIBUTE only, no element injection, no script execution", () => {
    const HOSTILE_CLASS =
      '"><img src=x onerror=window.__pwned=3><script>window.__pwned=4</script>';
    const { container } = render(
      <Kv label="Hostile class" valueClassName={HOSTILE_CLASS}>
        inert value
      </Kv>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(
      (window as unknown as { __pwned?: number }).__pwned,
    ).toBeUndefined();
    // The payload lands verbatim INSIDE the class attribute — never
    // parsed as markup.
    expect(screen.getByText("inert value").getAttribute("class")).toContain("onerror");
  });
});
