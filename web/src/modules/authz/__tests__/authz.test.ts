import { MODULES, isModuleId } from "../modules";
import { can, myPermissionsSchema } from "../schemas";

const fixture = {
  role_id: "8c8f13f2-0000-0000-0000-000000000000",
  permissions: [
    {
      module: "members",
      can_view: true,
      can_create: true,
      can_edit: false,
      can_approve: false,
    },
    {
      module: "loan_book",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
  ],
};

describe("authz", () => {
  it("parses the /me/permissions contract", () => {
    const parsed = myPermissionsSchema.parse(fixture);
    expect(parsed.permissions).toHaveLength(2);
  });

  it("rejects malformed permission payloads", () => {
    const bad = { role_id: "x", permissions: [{ module: "members", can_view: "yes" }] };
    expect(myPermissionsSchema.safeParse(bad).success).toBe(false);
  });

  it("is deny-by-default: unknown modules, missing data, unlisted actions", () => {
    const perms = myPermissionsSchema.parse(fixture);
    expect(can(perms, "members", "view")).toBe(true);
    expect(can(perms, "members", "create")).toBe(true);
    expect(can(perms, "members", "edit")).toBe(false);
    expect(can(perms, "loan_book", "approve")).toBe(false);
    expect(can(perms, "reports", "view")).toBe(false);
    expect(can(undefined, "members", "view")).toBe(false);
  });

  it("module ids mirror the backend Module enum", () => {
    expect(MODULES).toEqual([
      "members",
      "applications",
      "loan_book",
      "transactions",
      "reports",
      "settings",
      "access_control",
      // P13.15 (#31 batch 1, audit #30 R1): the corrections fraud
      // channel carries its OWN module, never generic transactions.
      // The backend's member_identity (P14.5) is DELIBERATELY absent:
      // it gates member-facing auth substrate routes with no console
      // surface — deny-by-default means an unknown module id simply
      // never grants anything here.
      "corrections",
    ]);
    expect(isModuleId("members")).toBe(true);
    expect(isModuleId("corrections")).toBe(true);
    expect(isModuleId("member_identity")).toBe(false);
    expect(isModuleId("dashboard")).toBe(false);
  });
});
