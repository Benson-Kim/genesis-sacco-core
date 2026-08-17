import { z } from "zod";

/** Response of GET /me/permissions — drives route guards and nav (gate 1.6). */
export const modulePermissionsSchema = z.object({
  module: z.string(),
  can_view: z.boolean(),
  can_create: z.boolean(),
  can_edit: z.boolean(),
  can_approve: z.boolean(),
});

export const myPermissionsSchema = z.object({
  role_id: z.string(),
  permissions: z.array(modulePermissionsSchema),
});

export type ModulePermissions = z.infer<typeof modulePermissionsSchema>;
export type MyPermissions = z.infer<typeof myPermissionsSchema>;

/** Deny-by-default lookup mirroring the server-side authz dependency. */
export function can(
  permissions: MyPermissions | undefined,
  module: string,
  action: "view" | "create" | "edit" | "approve",
): boolean {
  if (permissions === undefined) return false;
  const entry = permissions.permissions.find((p) => p.module === module);
  if (entry === undefined) return false;
  switch (action) {
    case "view":
      return entry.can_view;
    case "create":
      return entry.can_create;
    case "edit":
      return entry.can_edit;
    case "approve":
      return entry.can_approve;
  }
}
