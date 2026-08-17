/**
 * Clickjacking guard (carried from !25): the console refuses framed
 * execution. Falsifiable: remove the FrameGuard from the root layout or
 * neuter its probe and the "blocks" case fails.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { FrameGuard } from "../components/FrameGuard";

test("framed execution is refused — children never render", async () => {
  render(
    <FrameGuard isFramed={() => true}>
      <div data-testid="privileged">privileged console</div>
    </FrameGuard>,
  );
  await waitFor(() =>
    expect(screen.getByText(/cannot run inside a frame/)).toBeInTheDocument(),
  );
  expect(screen.queryByTestId("privileged")).toBeNull();
});

test("direct (unframed) execution renders the console", () => {
  render(
    <FrameGuard isFramed={() => false}>
      <div data-testid="privileged">privileged console</div>
    </FrameGuard>,
  );
  expect(screen.getByTestId("privileged")).toBeInTheDocument();
  expect(screen.queryByText(/cannot run inside a frame/)).toBeNull();
});
