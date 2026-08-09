import { z } from "zod";

/**
 * Backend error envelope (least disclosure): a sanitized category plus a
 * correlation ID — never internals or stack traces.
 */
export const errorEnvelopeSchema = z.object({
  category: z.string(),
  correlation_id: z.string(),
});

export type ErrorEnvelope = z.infer<typeof errorEnvelopeSchema>;

/**
 * One field-level validation message from a FastAPI 422 body.
 * (Salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238.)
 */
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
 * FastAPI `loc` heads naming WHERE a parameter lives. The head is
 * stripped to the documented canonical field key (W56-5): the key is
 * the dotted path WITHOUT its location head, so body and query errors
 * address the same form field identically and `form-errors.ts`
 * matching never silently misses a verdict.
 */
const LOCATION_HEADS = new Set(["body", "query", "path", "header", "cookie"]);

/**
 * Build an ApiError from an openapi-fetch error body + response. Handles
 * both the app envelope {category, correlation_id} and FastAPI validation
 * bodies {detail: [{loc, msg}]} so 422 stays DISTINCT from 409/other
 * conflicts and field messages reach the operator. Anything else degrades
 * to a sanitized internal error — never an echo of the body (least disclosure).
 *
 * Field keys are CANONICAL (W56-5): the leading location head — body,
 * query, path, header or cookie — is stripped (head position ONLY, so a
 * nested segment that happens to be named like a head survives); nested
 * segments join with ".".
 */
export function toApiError(error: unknown, response: Response): ApiError {
  const parsed = errorEnvelopeSchema.safeParse(error);
  if (parsed.success) {
    return new ApiError(
      response.status,
      parsed.data.category,
      parsed.data.correlation_id,
    );
  }
  const validation = fastApiDetailSchema.safeParse(error);
  if (validation.success) {
    const fields = validation.data.detail.map((item) => {
      const loc = (item.loc ?? []).map(String);
      const head = loc[0];
      const canonical = head !== undefined && LOCATION_HEADS.has(head) ? loc.slice(1) : loc;
      return {
        field: canonical.join("."),
        message: item.msg ?? "invalid value",
      };
    });
    return new ApiError(response.status, "validation_error", null, fields);
  }
  return new ApiError(response.status, "internal_error", null);
}
