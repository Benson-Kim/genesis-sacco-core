/**
 * Issue #8 sidebar rules, test-enforced: dual icon states (outline
 * inactive → solid filled active) and the bottom-anchored utility zone
 * for secondary/administrative links.
 */
import { render, screen } from "@testing-library/react";
import { NAV_SECTIONS } from "../nav";
import { NavIcon } from "../NavIcon";

describe("dual icon active states", () => {
  it("renders the FILLED variant only for the active page", () => {
    render(<NavIcon shape="members" active={true} />);
    const icon = screen.getByTestId("nav-icon-members");
    expect(icon).toHaveAttribute("data-variant", "filled");
    expect(icon).toHaveAttribute("fill", "currentColor");
  });

  it("renders the OUTLINE variant when inactive", () => {
    render(<NavIcon shape="members" active={false} />);
    const icon = screen.getByTestId("nav-icon-members");
    expect(icon).toHaveAttribute("data-variant", "outline");
    expect(icon).toHaveAttribute("fill", "none");
    expect(icon).toHaveAttribute("stroke", "currentColor");
  });

  it("is decorative for screen readers (the text label carries meaning)", () => {
    render(<NavIcon shape="reports" active={false} />);
    expect(screen.getByTestId("nav-icon-reports")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("nav structure", () => {
  it("every item carries a dual-state icon", () => {
    for (const section of NAV_SECTIONS) {
      for (const item of section.items) {
        expect(item.icon).toBeTruthy();
      }
    }
  });

  it("exactly one bottom-anchored utility section, and it is the last", () => {
    const utilities = NAV_SECTIONS.filter((section) => section.utility === true);
    expect(utilities).toHaveLength(1);
    expect(NAV_SECTIONS[NAV_SECTIONS.length - 1]?.utility).toBe(true);
  });

  it("hrefs are unique (no ambiguous active states)", () => {
    const hrefs = NAV_SECTIONS.flatMap((section) => section.items.map((item) => item.href));
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });
});
