/**
 * Field-error resolution for forms (one copy, reuse-first):
 * merges client-side Zod validation issues and server-side 422 messages
 * (ApiError.fields, parsed least-disclosure by toApiError) into a single
 * field → message map for FormField.
 *
 * Server messages WIN over client messages for the same field: the API
 * is the enforcer (least disclosure) and its verdict supersedes the local guess.
 */
import { ApiError } from "@genesis/api-client";
import type { ZodError } from "zod";

export type FieldErrors = Readonly<Record<string, string>>;

/** First message per field from a client-side Zod parse failure. */
export function fromZodError(error: ZodError): FieldErrors {
  const out: Record<string, string> = {};
  for (const issue of error.issues) {
    const key = issue.path.map(String).join(".");
    if (!(key in out)) out[key] = issue.message;
  }
  return out;
}

/** First message per field from a server 422 (empty for anything else). */
export function fromApiError(error: unknown): FieldErrors {
  if (!(error instanceof ApiError)) return {};
  const out: Record<string, string> = {};
  for (const field of error.fields) {
    if (!(field.field in out)) out[field.field] = field.message;
  }
  return out;
}

/** Merge client + server maps; the server verdict wins per field. */
export function mergeFieldErrors(client: FieldErrors, server: FieldErrors): FieldErrors {
  return { ...client, ...server };
}
