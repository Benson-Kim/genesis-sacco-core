/**
 * RBAC module identifiers — must match the backend `Module` enum
 * (backend/src/genesis/domain/rbac.py), itself seeded from the prototype
 * `modulesRB` (P4). `corrections` is the extension: the fraud
 * channel carries its OWN permission strings, never generic
 * transactions:edit (maker-checker). `member_identity` is
 * deliberately absent here: it gates member-facing auth substrate
 * routes and has no console surface.
 */
export const MODULES = [
  "members",
  "applications",
  "loan_book",
  "transactions",
  "reports",
  "settings",
  "access_control",
  "corrections",
] as const;

export type ModuleId = (typeof MODULES)[number];

export const MODULE_LABELS: Record<ModuleId, string> = {
  members: "Members",
  applications: "Applications",
  loan_book: "Loan book",
  transactions: "Transactions",
  reports: "Reports",
  settings: "Settings",
  access_control: "Access control",
  corrections: "Corrections",
};

export function isModuleId(value: string): value is ModuleId {
  return (MODULES as readonly string[]).includes(value);
}
