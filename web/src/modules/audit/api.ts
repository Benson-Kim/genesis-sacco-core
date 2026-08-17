/**
 * Audit-log API layer (P13.5) over the generated client.
 *
 * Keyset pagination ONLY (gate 1.3): the cursor is an opaque server
 * string, echoed back as a bound query parameter, never parsed, never
 * rendered. No offset/page parameters exist here (tested).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import { auditEntrySchema, type AuditEntry, type AuditFilters } from "./schemas";

export const AUDIT_PAGE_SIZE = 20;

const auditPageSchema = keysetPageSchema(auditEntrySchema);

export async function fetchAuditPage(
  filters: AuditFilters,
  cursor: string | null,
): Promise<KeysetPage<AuditEntry>> {
  const { data, error, response } = await api.GET("/audit-log", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: AUDIT_PAGE_SIZE,
        entity: filters.entity === "" ? undefined : filters.entity,
        actor_id: filters.actorId === "" ? undefined : filters.actorId,
        action: filters.action === "" ? undefined : filters.action,
        date_from: filters.dateFrom === "" ? undefined : filters.dateFrom,
        date_to: filters.dateTo === "" ? undefined : filters.dateTo,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return auditPageSchema.parse(data);
}
