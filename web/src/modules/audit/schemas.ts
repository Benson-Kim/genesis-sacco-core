import { z } from "zod";

/**
 * Audit-log entry (P13.5). `before`/`after` are redacted SERVER-SIDE per
 * role entitlement and pass through this boundary untouched (z.unknown()
 * — no reshaping, no defaults): the viewer renders them as escaped
 * pretty-printed JSON text exactly as the API returned them, with no
 * client-side reconstruction of redacted fields (gate 1.6).
 */
export const auditEntrySchema = z.object({
  id: z.number().int(),
  at: z.string(),
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
