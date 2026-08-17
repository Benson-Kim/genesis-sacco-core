/**
 * Access-control API layer — roles and per-role permissions matrix.
 *
 * Endpoints:
 *   GET  /access/roles                          — list all roles
 *   GET  /access/roles/{role_id}/permissions    — all module permissions for a role
 *   PUT  /access/roles/{role_id}/permissions/{module} — set one module's bits
 *
 * Permission changes are audit-logged server-side (gate 1.5).
 */
import { toApiError } from "@genesis/api-client";
import { api } from "@/lib/api";
import { z } from "zod";

export const permissionSchema = z.object({
  module: z.string(),
  can_view: z.boolean(),
  can_create: z.boolean(),
  can_edit: z.boolean(),
  can_approve: z.boolean(),
});

export type Permission = z.infer<typeof permissionSchema>;

export interface PermissionUpdate {
  can_view: boolean;
  can_create: boolean;
  can_edit: boolean;
  can_approve: boolean;
}

export async function fetchRolePermissions(roleId: string): Promise<Permission[]> {
  const { data, error, response } = await api.GET(
    "/access/roles/{role_id}/permissions",
    { params: { path: { role_id: roleId } } },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return z.array(permissionSchema).parse(data);
}

export async function updateRolePermission(
  roleId: string,
  module: string,
  body: PermissionUpdate,
): Promise<Permission> {
  const { data, error, response } = await api.PUT(
    "/access/roles/{role_id}/permissions/{module}",
    {
      params: { path: { role_id: roleId, module: module as never } },
      body,
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return permissionSchema.parse(data);
}
