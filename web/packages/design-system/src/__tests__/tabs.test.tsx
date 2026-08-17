import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TabList, TabPanel, type TabDef } from "../components/Tabs";

const TABS: TabDef[] = [
  { id: "a", label: "Alpha" },
  { id: "b", label: "Beta" },
  { id: "c", label: "Gamma" },
];

/** Stateful harness mirroring the real screen convention: caller owns
 * activeId and conditionally renders only the active tab's content. */
function Harness({ initial = "a" }: Readonly<{ initial?: string }>) {
  const [active, setActive] = useState(initial);
  return (
    <div>
      <TabList idPrefix="h" tabs={TABS} activeId={active} onChange={setActive} ariaLabel="Sections" />
      <TabPanel idPrefix="h" activeTabId={active}>
        {active === "a" && <div>Alpha content</div>}
        {active === "b" && <div>Beta content</div>}
        {active === "c" && <div>Gamma content</div>}
      </TabPanel>
    </div>
  );
}

describe("TabList/TabPanel (conforming ARIA tabs — promoted from the settings-screen module-local copy)", () => {
  it("FM1: renders one tab per entry, aria-selected on the active one only, and the matching panel content", () => {
    render(<Harness />);
    const alpha = screen.getByRole("tab", { name: "Alpha" });
    const beta = screen.getByRole("tab", { name: "Beta" });
    expect(alpha).toHaveAttribute("aria-selected", "true");
    expect(beta).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("Alpha content")).toBeInTheDocument();
    expect(screen.queryByText("Beta content")).toBeNull();
  });

  it("FM2: tab <-> tabpanel association (aria-controls / aria-labelledby / id) and roving tabindex", () => {
    render(<Harness />);
    const alpha = screen.getByRole("tab", { name: "Alpha" });
    const panel = screen.getByRole("tabpanel");
    expect(alpha).toHaveAttribute("aria-controls", panel.id);
    expect(panel).toHaveAttribute("aria-labelledby", alpha.id);
    expect(alpha).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Beta" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tab", { name: "Gamma" })).toHaveAttribute("tabindex", "-1");
  });

  it("FM3: clicking a tab selects it and swaps the panel content", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("tab", { name: "Beta" }));
    expect(screen.getByRole("tab", { name: "Beta" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Beta content")).toBeInTheDocument();
    expect(screen.queryByText("Alpha content")).toBeNull();
  });

  it("FM4: End/Home move BOTH selection and focus (automatic activation); ArrowLeft/ArrowRight wrap", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    screen.getByRole("tab", { name: "Alpha" }).focus();

    await user.keyboard("{End}");
    const gamma = screen.getByRole("tab", { name: "Gamma" });
    expect(gamma).toHaveAttribute("aria-selected", "true");
    expect(gamma).toHaveFocus();

    // Wraps forward past the last tab back to the first.
    await user.keyboard("{ArrowRight}");
    const alpha = screen.getByRole("tab", { name: "Alpha" });
    expect(alpha).toHaveAttribute("aria-selected", "true");
    expect(alpha).toHaveFocus();

    // Wraps backward past the first tab to the last.
    await user.keyboard("{ArrowLeft}");
    expect(gamma).toHaveAttribute("aria-selected", "true");
    expect(gamma).toHaveFocus();

    await user.keyboard("{Home}");
    expect(alpha).toHaveAttribute("aria-selected", "true");
    expect(alpha).toHaveFocus();
  });

  it("FM5: idPrefix namespaces ids — two independent tab groups on one page never collide", () => {
    render(
      <>
        <TabList idPrefix="one" tabs={TABS} activeId="a" onChange={() => undefined} ariaLabel="One" />
        <TabList idPrefix="two" tabs={TABS} activeId="a" onChange={() => undefined} ariaLabel="Two" />
      </>,
    );
    const ids = screen.getAllByRole("tab", { name: "Alpha" }).map((el) => el.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("FM6: hostile label strings render as inert TEXT — no element injection, no script execution", () => {
    const hostile = '<img src=x onerror=window.__pwned=1> "quoted" tab';
    const { container } = render(
      <TabList
        idPrefix="x"
        tabs={[{ id: "h", label: hostile }]}
        activeId="h"
        onChange={() => undefined}
        ariaLabel="Hostile"
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(screen.getByText(hostile)).toBeInTheDocument();
  });
});
