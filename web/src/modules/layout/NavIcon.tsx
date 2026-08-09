/**
 * Dual-state nav icons: the SAME geometry renders as an
 * OUTLINE when inactive and switches to the SOLID-FILLED variant only for
 * the active page — a distinct visual anchor that does not rely on color
 * alone (the active nav item also gains weight + background). Inline SVG
 * keeps the dependency lockfile untouched (no icon package, no CDN).
 */
export type NavIconShape =
  | "dashboard"
  | "members"
  | "applications"
  | "loan_book"
  | "transactions"
  | "reports"
  | "settings"
  | "access_control"
  | "audit_log"
  | "committee"
  | "guarantors"
  | "exit";

const ICON_PATHS: Record<NavIconShape, string> = {
  dashboard: "M4 4h7v7H4zM13 4h7v4h-7zM13 10h7v10h-7zM4 13h7v7H4z",
  members:
    "M12 5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7zM5 19c0-3.3 3.1-5 7-5s7 1.7 7 5v1H5z",
  applications: "M6 3h9l4 4v14H6zM14 3v5h5",
  loan_book: "M5 4h6a3 3 0 0 1 3 3v13a3 3 0 0 0-3-3H5zM19 4h-5v13h5z",
  transactions: "M4 8h13l-3-3M20 16H7l3 3",
  reports: "M5 20V10M10 20V4M15 20v-8M20 20v-4",
  settings:
    "M12 8.5a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7zM12 2l1 3h-2zM12 22l-1-3h2zM2 12l3-1v2zM22 12l-3 1v-2zM4.9 4.9l2.8 1.4-1.4 1.4zM19.1 19.1l-2.8-1.4 1.4-1.4zM19.1 4.9l-1.4 2.8-1.4-1.4zM4.9 19.1l1.4-2.8 1.4 1.4z",
  access_control: "M7 11V7a5 5 0 0 1 10 0v4M5 11h14v10H5z",
  // Ledger/journal sheet with entry lines — the audit-trail metaphor.
  audit_log: "M6 3h12v18H6zM9 8h6M9 12h6M9 16h4",
  // Ballot box with a cast slip — the committee-vote metaphor.
  committee: "M4 11h16v9H4zM8 11V8h8v3M10 4h4v4h-4zM4 15h16",
  // Shield over a person — the guarantor-collateral metaphor.
  guarantors:
    "M12 2l8 3v6c0 5-3.5 8.5-8 11-4.5-2.5-8-6-8-11V5zM12 7.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM7.5 17c.9-1.9 2.6-3 4.5-3s3.6 1.1 4.5 3",
  // Open door with an outbound arrow — the member-exit metaphor.
  exit: "M14 3h6v18h-6M14 3v18M3 12h9M9 8l-4 4 4 4",
};

export function NavIcon({ shape, active }: { shape: NavIconShape; active: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      aria-hidden="true"
      data-testid={`nav-icon-${shape}`}
      data-variant={active ? "filled" : "outline"}
      fill={active ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth={active ? 0 : 1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={ICON_PATHS[shape]} />
    </svg>
  );
}
