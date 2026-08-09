import { z } from "zod";
import { isoTimestampSchema } from "@/lib/schemas";

/**
 * Audit-log entry (salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238).
 * `before`/`after` are redacted SERVER-SIDE per
 * role entitlement and pass through this boundary untouched (z.unknown()
 * — no reshaping, no defaults): the viewer renders them as escaped
 * pretty-printed JSON text exactly as the API returned them, with no
 * client-side reconstruction of redacted fields (least disclosure).
 */
export const auditEntrySchema = z.object({
  id: z.number().int(),
  /** `datetime.isoformat()` shape (api/audit_log.py `at=entry.at.isoformat()`)
   * — feeds fmtDateTime in the register row AND the detail drawer
   * (AuditScreen.tsx): a garbage value is REJECTED at the boundary,
   * never rendered "Invalid Date" (retrofit). */
  at: isoTimestampSchema,
  actor_id: z.string().nullable(),
  action: z.string(),
  entity: z.string(),
  entity_id: z.string(),
  before: z.unknown(),
  after: z.unknown(),
  redacted: z.boolean(),
});

export type AuditEntry = z.infer<typeof auditEntrySchema>;

export interface AuditFilters {
  entity: string;
  actorId: string;
  action: string;
  dateFrom: string;
  dateTo: string;
}

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  entity: "",
  actorId: "",
  action: "",
  dateFrom: "",
  dateTo: "",
};
