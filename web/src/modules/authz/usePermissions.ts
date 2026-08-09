"use client";

import { useQuery } from "@tanstack/react-query";
import { toApiError } from "@genesis/api-client";
import { myPermissionsSchema, type MyPermissions } from "./schemas";
import { api } from "@/lib/api";

export const PERMISSIONS_QUERY_KEY = ["me", "permissions"] as const;

async function fetchMyPermissions(): Promise<MyPermissions> {
  const { data, error, response } = await api.GET("/me/permissions");
  if (error !== undefined || data === undefined) {
    throw toApiError(error, response);
  }
  return myPermissionsSchema.parse(data);
}

/**
 * Permissions of the signed-in user. The UI hides what the API forbids,
 * but the API remains the enforcer on every call (least disclosure).
 */
export function usePermissions() {
  return useQuery({
    queryKey: PERMISSIONS_QUERY_KEY,
    queryFn: fetchMyPermissions,
    staleTime: 60_000,
  });
}
