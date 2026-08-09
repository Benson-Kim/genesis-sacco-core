/**
 * ConfirmDangerModal proofs (P15 Phase B). Falsifiable: allow confirm
 * without the typed phrase, allow a near-miss/case-insensitive match,
 * drop the pending short-circuit, or render the phrase through a parser
 * sink and the matching test fails.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfirmDangerModal } from "../components/ConfirmDangerModal";

function mount(props: Partial<Parameters<typeof ConfirmDangerModal>[0]> = {}) {
  const onConfirm = jest.fn();
  const onClose = jest.fn();
  render(
    <ConfirmDangerModal
      title="Suspend user"
      confirmPhrase="GP-0001"
      confirmLabel="Suspend user"
      onConfirm={onConfirm}
      onClose={onClose}
      {...props}
    />,
  );
  return { onConfirm, onClose };
}

test("confirm stays disabled until the EXACT phrase is typed (case-sensitive)", async () => {
  const user = userEvent.setup();
  const { onConfirm } = mount();
  const confirm = screen.getByRole("button", { name: "Suspend user" });
  const input = screen.getByLabelText('Type "GP-0001" to confirm');

  expect(confirm).toBeDisabled();
  await user.type(input, "gp-0001"); // near miss: wrong case
  expect(confirm).toBeDisabled();
  await user.clear(input);
  await user.type(input, "GP-0001 "); // near miss: trailing space
  expect(confirm).toBeDisabled();
  await user.clear(input);
  await user.type(input, "GP-0001");
  expect(confirm).toBeEnabled();

  await user.click(confirm);
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

test("homoglyph twin REFUSED (#31 ledger (h2)): a Cyrillic look-alike phrase never arms confirm — the compare is byte-identical, not visual", async () => {
  const user = userEvent.setup();
  const { onConfirm } = mount();
  const confirm = screen.getByRole("button", { name: "Suspend user" });
  const input = screen.getByLabelText('Type "GP-0001" to confirm');

  // "GР-0001": U+0420 CYRILLIC CAPITAL LETTER ER in place of ASCII
  // "P" (U+0050) — visually identical in most UI fonts, byte-distinct.
  // Hand-computed oracle: the twin differs at index 1 and ONLY there.
  const homoglyph = "G\u0420-0001";
  expect(homoglyph).not.toBe("GP-0001");
  expect(homoglyph.charCodeAt(1)).toBe(0x0420);
  expect(homoglyph.length).toBe("GP-0001".length);

  // Delivered by PASTE — the clipboard is exactly how homoglyphs
  // arrive (a chat message, a lookalike email); typing produces the
  // keyboard's real code points.
  await user.click(input);
  await user.paste(homoglyph);
  expect(input).toHaveValue(homoglyph);
  expect(confirm).toBeDisabled();
  await user.click(confirm);
  expect(onConfirm).not.toHaveBeenCalled();

  // The guard is EXACTNESS, not lockout: the genuine ASCII phrase
  // still arms after clearing the twin.
  await user.clear(input);
  await user.type(input, "GP-0001");
  expect(confirm).toBeEnabled();
});

test("pending blocks re-confirmation AND every dismissal path", async () => {
  const user = userEvent.setup();
  const { onConfirm, onClose } = mount({ pending: true });

  expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
  await user.keyboard("{Escape}");
  expect(onClose).not.toHaveBeenCalled();
  expect(onConfirm).not.toHaveBeenCalled();
});

test("a hostile confirmation phrase renders as inert text (no sink)", () => {
  const hostile = "<img src=x onerror=alert(1)>";
  const { container } = ((): { container: HTMLElement } => {
    const onConfirm = jest.fn();
    return render(
      <ConfirmDangerModal
        title="Remove record"
        confirmPhrase={hostile}
        confirmLabel="Remove"
        onConfirm={onConfirm}
        onClose={jest.fn()}
      />,
    );
  })();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getAllByText(hostile, { exact: false }).length).toBeGreaterThan(0);
});
