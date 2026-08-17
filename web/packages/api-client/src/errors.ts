import { z } from "zod";

/**
 * Backend error envelope (gate 1.6): a sanitized category plus a
 * correlation ID — never internals or stack traces.
 */
export const errorEnvelopeSchema = z.object({
  category: z.string(),
  correlation_id: z.string(),
});

export type ErrorEnvelope = z.infer<typeof errorEnvelopeSchema>;

/** One field-level validation message from a FastAPI 422 body. */
export interface FieldError {
  field: string;
  message: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly category: string;
  readonly correlationId: string | null;
  /** Field-level 422 details (empty for every other error shape). */
  readonly fields: readonly FieldError[];

  constructor(
    status: number,
    category: string,
    correlationId: string | null,
    fields: readonly FieldError[] = [],
  ) {
    super(`API error ${status}: ${category}`);
    this.name = "ApiError";
    this.status = status;
    this.category = category;
    this.correlationId = correlationId;
    this.fields = fields;
  }
}

/** FastAPI validation body — parsed without ever trusting its shape. */
const fastApiDetailSchema = z.object({
  detail: z.array(
    z.object({
      loc: z.array(z.union([z.string(), z.number()])).optional(),
      msg: z.string().optional(),
    }),
  ),
});

/**
 * Build an ApiError from an openapi-fetch error body + response. Handles
 * both the app envelope {category, correlation_id} and FastAPI validation
 * bodies {detail: [{loc, msg}]} so 422 stays DISTINCT from 409/other
 * conflicts and field messages reach the operator (ported from !25).
 */
export function toApiError(error: unknown, response: Response): ApiError {
  const parsed = errorEnvelopeSchema.safeParse(error);
  if (parsed.success) {
    return new ApiError(response.status, parsed.data.category, parsed.data.correlation_id);
  }
  const validation = fastApiDetailSchema.safeParse(error);
  if (validation.success) {
    const fields = validation.data.detail.map((item) => ({
      field: (item.loc ?? []).filter((part) => part !== "body").join("."),
      message: item.msg ?? "invalid value",
    }));
    return new ApiError(response.status, "validation_error", null, fields);
  }
  return new ApiError(response.status, "internal_error", null);
}
