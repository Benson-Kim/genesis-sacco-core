/**
 * Responsiveness proofs (P15 Phase B). jsdom does not evaluate media
 * queries, so these are STATIC assertions over the stylesheets and their
 * consumers — falsifiable: drop a breakpoint block, inline a private
 * grid again, or remove the table scroll strategy and a test fails.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = join(__dirname, "..", "..", "..");

function read(relPath: string): string {
  return readFileSync(join(SRC, relPath), "utf8");
}

describe("shared responsive grid (grid.module.css)", () => {
  const grid = read("modules/layout/grid.module.css");

  it("defines BOTH convention breakpoints (960px medium, 640px narrow)", () => {
    expect(grid).toContain("@media (max-width: 960px)");
    expect(grid).toContain("@media (max-width: 640px)");
  });

  it("defines the two standard page patterns", () => {
    expect(grid).toContain(".cards4");
    expect(grid).toContain(".sideMain");
  });

  it("the dashboard consumes the SHARED grid (no private page grid)", () => {
    const screen = read("modules/dashboard/components/DashboardScreen.tsx");
    expect(screen).toContain('from "@/modules/layout/grid.module.css"');
    expect(screen).toContain("grid.cards4");
    const css = read("modules/dashboard/components/DashboardScreen.module.css");
    expect(css).not.toMatch(/grid-template-columns:\s*repeat\(4/);
  });

  it("the permissions matrix consumes the SHARED side+main grid", () => {
    const screen = read("modules/access/PermissionsScreen.tsx");
    expect(screen).toContain("grid.sideMain");
    const css = read("modules/access/PermissionsScreen.module.css");
    expect(css).not.toMatch(/grid-template-columns:\s*280px/);
  });
});

describe("KeysetTable small-viewport strategy", () => {
  const css = read("modules/table/KeysetTable.module.css");

  it("scroll container with a visible affordance + narrow min-width fallback", () => {
    expect(css).toContain(".scroller");
    expect(css).toContain("overflow-x: auto");
    // Visible affordance: CSS scroll shadows.
    expect(css).toContain("background-attachment: local, local, scroll, scroll");
    expect(css).toContain("@media (max-width: 640px)");
    expect(css).toMatch(/min-width:\s*\d+px/);
  });

  it("the focusable scroll region keeps a visible focus ring (issue #8)", () => {
    expect(css).toContain(".scroller:focus-visible");
  });
});

describe("Modal narrow-viewport strategy", () => {
  it("drawers/dialogs go full-screen under the narrow breakpoint", () => {
    const css = readFileSync(
      join(SRC, "..", "packages", "design-system", "src", "components", "Modal.module.css"),
      "utf8",
    );
    expect(css).toContain("@media (max-width: 640px)");
    expect(css).toContain("100vw");
  });
});
