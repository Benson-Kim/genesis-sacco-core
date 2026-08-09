/**
 * Modal focus-management proofs (issue #8; review finding F-A2).
 * Falsifiable: remove the trap (Tab escapes the dialog), the initial
 * focus, the return-focus effect, or the Escape handling and the matching
 * test fails.
 */
import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "../components/Modal";

function Harness({
  closeDisabled = false,
  dismissOnOverlay,
}: Readonly<{ closeDisabled?: boolean; dismissOnOverlay?: boolean }>) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Open drawer
      </button>
      {open && (
        <Modal
          title="Test drawer"
          onClose={() => setOpen(false)}
          closeDisabled={closeDisabled}
          {...(dismissOnOverlay === undefined ? {} : { dismissOnOverlay })}
        >
          <input aria-label="First field" />
          <button type="button">Middle action</button>
          <button type="button">Last action</button>
        </Modal>
      )}
    </div>
  );
}

test("focus moves INTO the dialog on open (not left on the trigger)", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Open drawer" }));
  const dialog = screen.getByRole("dialog", { name: "Test drawer" });
  await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
});

test("Tab is trapped: cycling forward from the last element wraps to the first", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Open drawer" }));

  const last = screen.getByRole("button", { name: "Last action" });
  last.focus();
  await user.tab();
  // Wrapped to the first focusable inside the dialog (the ✕ button),
  // never out to the page behind.
  const dialog = screen.getByRole("dialog", { name: "Test drawer" });
  expect(dialog.contains(document.activeElement)).toBe(true);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Close" }));
});

test("Shift+Tab is trapped: cycling back from the first element wraps to the last", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Open drawer" }));

  screen.getByRole("button", { name: "Close" }).focus();
  await user.tab({ shift: true });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Last action" }));
});

test("Escape closes the modal and focus RETURNS to the opener", async () => {
  const user = userEvent.setup();
  render(<Harness />);
  const opener = screen.getByRole("button", { name: "Open drawer" });
  await user.click(opener);
  expect(screen.getByRole("dialog", { name: "Test drawer" })).toBeInTheDocument();

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "Test drawer" })).toBeNull();
  await waitFor(() => expect(document.activeElement).toBe(opener));
});

test("Escape is ignored while closeDisabled (in-flight mutation must not lose state)", async () => {
  const user = userEvent.setup();
  render(<Harness closeDisabled />);
  await user.click(screen.getByRole("button", { name: "Open drawer" }));

  await user.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "Test drawer" })).toBeInTheDocument();
});

test("overlay click dismisses by default (read-only drawers keep the light dismissal)", async () => {
  const user = userEvent.setup();
  const { container } = render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Open drawer" }));
  expect(screen.getByRole("dialog", { name: "Test drawer" })).toBeInTheDocument();

  const overlay = container.querySelector('[role="presentation"]');
  expect(overlay).not.toBeNull();
  await user.click(overlay as HTMLElement);
  expect(screen.queryByRole("dialog", { name: "Test drawer" })).toBeNull();
});

test("W56-3: dismissOnOverlay={false} — a stray overlay click on a dirty form does NOT close (no silent data loss); ✕ still closes", async () => {
  const user = userEvent.setup();
  const { container } = render(<Harness dismissOnOverlay={false} />);
  await user.click(screen.getByRole("button", { name: "Open drawer" }));

  // The operator has typed into the form (dirty state)…
  await user.type(screen.getByRole("textbox", { name: "First field" }), "half-completed entry");

  // …and a stray click outside the drawer must NOT discard it.
  const overlay = container.querySelector('[role="presentation"]');
  expect(overlay).not.toBeNull();
  await user.click(overlay as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Test drawer" })).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "First field" })).toHaveValue(
    "half-completed entry",
  );

  // Explicit dismissal remains available.
  await user.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Test drawer" })).toBeNull();
});
