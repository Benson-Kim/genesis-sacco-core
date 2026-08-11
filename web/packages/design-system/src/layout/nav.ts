import type { ModuleId } from "@/modules/authz/modules";
import type { NavIconShape } from "./NavIcon";

export interface NavItem {
  label: string;
  href: string;
  /** RBAC module gating visibility; null = visible to any signed-in user. */
  module: ModuleId | null;
  /** Dual-state icon: outline inactive → filled active. */
  icon: NavIconShape;
}

export interface NavSection {
  label: string;
  items: NavItem[];
  /**
   * Bottom-anchored utility zone: secondary/administrative
   * links pin to the sidebar footer, keeping core navigation clean.
   */
  utility?: boolean;
}

/**
 * Sidebar structure mirroring the prototype NAV. Prototype entries
 * without their own RBAC module (guarantors, committee, member exit,
 * recovery) live under their owning modules.
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Operations",
    items: [
      { label: "Dashboard", href: "/dashboard", module: null, icon: "dashboard" },
      { label: "Members", href: "/modules/members", module: "members", icon: "members" },
      {
        label: "Applications",
        href: "/modules/applications",
        module: "applications",
        icon: "applications",
      },
      { label: "Loan book", href: "/modules/loan_book", module: "loan_book", icon: "loan_book" },
      {
        label: "Guarantors",
        href: "/modules/applications/guarantors",
        module: "applications",
        icon: "guarantors",
      },
      {
        label: "Transactions",
        href: "/modules/transactions",
        module: "transactions",
        icon: "transactions",
      },
    ],
  },
  {
    label: "Governance",
    items: [
      {
        label: "Credit committee",
        href: "/modules/applications/committee",
        module: "applications",
        icon: "committee",
      },
      {
        label: "Member exit",
        href: "/modules/members/exits",
        module: "members",
        icon: "exit",
      },
      {
        //  dormancy worklist:
        // the register's own dormant filter + the members:edit
        // operations run. Lives under the members RBAC module (the
        // member-exit precedent; no dedicated dormancy module exists
        // in the P4 matrix).
        label: "Dormancy",
        href: "/modules/members/dormancy",
        module: "members",
        icon: "members",
      },
      {
        // Share-transfers console: the share lifecycle's exit
        // path. Lives under the members RBAC module (the route is
        // gated members:approve server-side — there is no dedicated
        // shares module in the P4 matrix; the member-exit-under-
        // members precedent).
        label: "Share transfers",
        href: "/modules/members/share-transfers",
        module: "members",
        icon: "members",
      },
      {
        // The dividends lifecycle console;
        // lives under the transactions RBAC module (every /dividends
        // route is gated on transactions view/edit/approve server-side
        // — there is no dedicated dividends module in the P4 matrix).
        label: "Dividends",
        href: "/modules/transactions/dividends",
        module: "transactions",
        icon: "transactions",
      },
      {
        // Corrections console: the fraud channel's DEDICATED corrections RBAC module,
        // never generic transactions (maker-checker).
        label: "Corrections",
        href: "/modules/corrections",
        module: "corrections",
        icon: "transactions",
      },
      {
        // Accounting-periods console: period-close visibility + the approve-gated close.
        // Lives under the transactions RBAC module (every
        // /accounting-periods route is gated on transactions
        // view/approve server-side — the dividends precedent; there is
        // no dedicated periods module in the P4 matrix).
        label: "Accounting periods",
        href: "/modules/transactions/periods",
        module: "transactions",
        icon: "transactions",
      },
      {
        //  recovery worklist;
        // lives under the loan_book RBAC module (every /recovery-cases
        // route is gated on loan_book view/create/edit server-side —
        // there is no dedicated recovery module in the P4 matrix; the
        // member-exit-under-members precedent).
        label: "Recovery",
        href: "/modules/loan_book/recovery",
        module: "loan_book",
        icon: "loan_book",
      },
    ],
  },
  {
    label: "Insights",
    items: [{ label: "Reports", href: "/modules/reports", module: "reports", icon: "reports" }],
  },
  {
    label: "Administration",
    utility: true,
    items: [
      { label: "Settings", href: "/modules/settings", module: "settings", icon: "settings" },
      {
        //  branches registry console: the
        // registry CRUD sits under settings view/create/edit per the
        // P4 matrix (the prototype manages branches from Settings);
        // people assignments in the drawer follow their OWN modules.
        label: "Branches",
        href: "/modules/settings/branches",
        module: "settings",
        icon: "settings",
      },
      {
        label: "Access control",
        href: "/modules/access_control",
        module: "access_control",
        icon: "access_control",
      },
      {
        label: "Audit log",
        href: "/modules/access_control/audit-log",
        module: "access_control",
        icon: "audit_log",
      },
    ],
  },
];
