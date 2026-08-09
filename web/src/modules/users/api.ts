/**
 * Users administration API layer (Access-control module) over the
 * GENERATED client — salvaged from duo/feature/p13-5-frontend-followthrough
 * @ 198a238.
 *
 * - Keyset pagination ONLY: opaque `cursor` echoed back verbatim; no
 *   offset/page parameters exist here (scalability, tested).
 * - Every mutation takes a caller-supplied Idempotency-Key following the
 *   stability/rotation contract (concurrency safety) and sends the `version` the
 *   record was loaded with — a stale write surfaces as 409 (never a
 *   silent overwrite).
 * - Ids travel as path parameters serialized by the generated client;
 *   tokens/PII never enter URLs (least disclosure, tested).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  otpInvalidateSchema,
  otpReenrolSchema,
  rolesSchema,
  userSchema,
  type OtpInvalidateResult,
  type OtpReenrolResult,
  type Role,
  type User,
  type UserStatus,
} from "./schemas";

export const USERS_PAGE_SIZE = 20;

const userPageSchema = keysetPageSchema(userSchema);

export interface UserListFilters {
  status: UserStatus | "";
  roleId: string;
}

export async function fetchUsersPage(
  filters: UserListFilters,
  cursor: string | null,
): Promise<KeysetPage<User>> {
  const { data, error, response } = await api.GET("/users", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: USERS_PAGE_SIZE,
        status: filters.status === "" ? undefined : filters.status,
        role_id: filters.roleId === "" ? undefined : filters.roleId,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userPageSchema.parse(data);
}

export async function fetchUser(userId: string): Promise<User> {
  const { data, error, response } = await api.GET("/users/{user_id}", {
    params: { path: { user_id: userId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userSchema.parse(data);
}

export async function fetchRoles(): Promise<Role[]> {
  const { data, error, response } = await api.GET("/access/roles");
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return rolesSchema.parse(data);
}

export interface CreateUserInput {
  full_name: string;
  email: string;
  role_id: string;
  phone: string | null;
  branch: string | null;
}

export async function createUser(input: CreateUserInput, idempotencyKey: string): Promise<User> {
  const { data, error, response } = await api.POST("/users", {
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userSchema.parse(data);
}

/**
 * Profile-only update (role/status have dedicated guarded routes). Fields
 * left undefined are NOT sent: the API treats absent/null as "keep the
 * current value" (review note — clearing a saved optional field is not yet supported server-side; the form surfaces this honestly).
 */
export interface UpdateUserInput {
  version: number;
  full_name?: string;
  email?: string;
  phone?: string;
  branch?: string;
}

export async function updateUser(
  userId: string,
  input: UpdateUserInput,
  idempotencyKey: string,
): Promise<User> {
  const { data, error, response } = await api.PUT("/users/{user_id}", {
    params: { path: { user_id: userId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userSchema.parse(data);
}

export async function changeUserStatus(
  userId: string,
  input: { version: number; status: UserStatus },
  idempotencyKey: string,
): Promise<User> {
  const { data, error, response } = await api.POST("/users/{user_id}/status", {
    params: { path: { user_id: userId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userSchema.parse(data);
}

export async function assignUserRole(
  userId: string,
  input: { version: number; role_id: string },
  idempotencyKey: string,
): Promise<User> {
  const { data, error, response } = await api.POST("/users/{user_id}/role", {
    params: { path: { user_id: userId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userSchema.parse(data);
}

export async function invalidateUserOtp(
  userId: string,
  idempotencyKey: string,
): Promise<OtpInvalidateResult> {
  const { data, error, response } = await api.POST("/users/{user_id}/otp/invalidate", {
    params: { path: { user_id: userId } },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return otpInvalidateSchema.parse(data);
}

export async function reenrolUserOtp(
  userId: string,
  idempotencyKey: string,
): Promise<OtpReenrolResult> {
  const { data, error, response } = await api.POST("/users/{user_id}/otp/reenrol", {
    params: { path: { user_id: userId } },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return otpReenrolSchema.parse(data);
}
