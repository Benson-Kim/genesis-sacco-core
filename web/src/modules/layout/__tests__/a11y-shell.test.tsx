/**
 * Shell accessibility primitives (P15 Phase B, issue #8). Falsifiable:
 * remove the live region, the skip link, or the main-content anchor and
 * the matching test fails.
 */
import { act, render, screen } from "@testing-library/react";
import { LiveAnnouncer, announce } from "../announcer";
import { MAIN_CONTENT_ID, SkipToContent } from "../SkipToContent";

describe("LiveAnnouncer", () => {
  it("announces messages into a status live region without moving focus", () => {
    render(
      <>
        <button type="button">anchor</button>
        <LiveAnnouncer />
      </>,
    );
    const before = document.activeElement;

    act(() => announce("Profile saved."));
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveAttribute("aria-atomic", "true");
    expect(region).toHaveTextContent("Profile saved.");
    expect(document.activeElement).toBe(before);
  });

  it("re-announces an identical repeated message (seq-keyed node)", () => {
    render(<LiveAnnouncer />);
    act(() => announce("Done."));
    const first = screen.getByText("Done.");
    act(() => announce("Done."));
    const second = screen.getByText("Done.");
    // A NEW text node was mounted (key changed) — AT re-reads it.
    expect(second).not.toBe(first);
  });

  it("supports assertive announcements for errors", () => {
    render(<LiveAnnouncer />);
    act(() => announce("Save failed.", "assertive"));
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "assertive");
  });
});

describe("SkipToContent", () => {
  it("links to the main content anchor", () => {
    render(<SkipToContent />);
    const link = screen.getByRole("link", { name: "Skip to content" });
    expect(link).toHaveAttribute("href", `#${MAIN_CONTENT_ID}`);
  });
});
