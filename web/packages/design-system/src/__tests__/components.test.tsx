import { render, screen } from "@testing-library/react";
import { Banner } from "../components/Banner";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Field } from "../components/Field";
import { Pill } from "../components/Pill";
import { Stat } from "../components/Stat";

describe("design-system primitives", () => {
  it("Button renders variants and honors disabled", () => {
    render(
      <>
        <Button>Plain</Button>
        <Button variant="primary">Save</Button>
        <Button variant="success" disabled>
          Approve
        </Button>
      </>,
    );
    expect(screen.getByRole("button", { name: "Plain" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("Card renders children", () => {
    render(<Card>card body</Card>);
    expect(screen.getByText("card body")).toBeInTheDocument();
  });

  it("Pill renders its status text", () => {
    render(<Pill bg="emeraldSoft" color="emerald">Normal</Pill>);
    expect(screen.getByText("Normal")).toBeInTheDocument();
  });

  it("Stat shows label and value", () => {
    render(<Stat label="Members" value="1,284" />);
    expect(screen.getByText("Members")).toBeInTheDocument();
    expect(screen.getByText("1,284")).toBeInTheDocument();
  });

  it("Field associates the label with its control", () => {
    render(
      <Field label="Phone or email" htmlFor="contact">
        <input id="contact" />
      </Field>,
    );
    expect(screen.getByLabelText("Phone or email")).toBeInTheDocument();
  });

  it("Banner live-region convention (W56-4): error is an assertive alert; info/ok are polite status regions; all atomic", () => {
    render(
      <>
        <Banner variant="error">Save failed.</Banner>
        <Banner variant="info">Record reloaded.</Banner>
        <Banner variant="ok">Saved.</Banner>
      </>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Save failed.");
    expect(alert).toHaveAttribute("aria-atomic", "true");
    // Non-error notices are LIVE (polite) — screen readers hear
    // conflict-withdrawal/reload notices without a separate announce().
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[0]).toHaveTextContent("Record reloaded.");
    expect(statuses[1]).toHaveTextContent("Saved.");
    for (const region of statuses) {
      expect(region).toHaveAttribute("aria-atomic", "true");
    }
  });
});
