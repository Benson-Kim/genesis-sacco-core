import type { ModuleId } from "@/modules/authz/modules";

export interface NavItem {
  label: string;
  href: string;
  /** RBAC module gating visibility; null = visible to any signed-in user. */
  module: ModuleId | null;
}

export interface NavSection {
  label: string;
  items: NavItem[];
}

/**
 * Sidebar structure mirroring the prototype NAV. Screens land in P15;
 * prototype entries without their own RBAC module (guarantors, committee,
 * member exit) will live under their owning modules when built.
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Operations",
    items: [
      { label: "Dashboard", href: "/dashboard", module: null },
      { label: "Members", href: "/modules/members", module: "members" },
      { label: "Applications", href: "/modules/applications", module: "applications" },
      { label: "Loan book", href: "/modules/loan_book", module: "loan_book" },
      { label: "Transactions", href: "/modules/transactions", module: "transactions" },
    ],
  },
  {
    label: "Insights",
    items: [{ label: "Reports", href: "/modules/reports", module: "reports" }],
  },
  {
    label: "Administration",
    items: [
      { label: "Settings", href: "/modules/settings", module: "settings" },
      { label: "Access control", href: "/modules/access_control", module: "access_control" },
      { label: "Audit log", href: "/modules/access_control/audit-log", module: "access_control" },
    ],
  },
];
