/**
 * Member-KYC API layer (profile and document-checklist routes) over the GENERATED client.
 *
 * - Permission strings are the members module's own (server-enforced, least disclosure): profile/document CREATION under members:create (they
 *   are registration wizard steps), reads under members:view (every profile read and checklist read is access-AUDITED server-side — / blocker f), edits under members:edit.
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the MATERIAL rule (web/README.md, concurrency safety): simple creates fold
 *   the op discriminator plus the full canonical body; versioned
 *   writes additionally fold the record version being acted on.
 * - The document list is the ONLY keyset read (scalability: opaque `cursor` echoed back verbatim; no offset or page parameters).
 * - Document UPDATE follows the contract's exclude-unset semantics
 *   only keys the operator actually set are sent —
 *   an omitted expiry keeps the stored one, an EXPLICIT null clears
 *   a wrongly-entered date.
 * - Ids travel as path parameters serialized by the generated client;
 *   bearer/tenant/idempotency values travel as HEADERS ONLY and no
 *   PII ever enters a URL (least disclosure).
 * - NO money moves here: the single monetary-looking field (declared
 *   monthly income) is an informational string inside the profile
 *   payload, constrained by the boundary grammar in kycSchemas.ts.
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  kycDocumentSchema,
  kycProfileSchema,
  type DocumentStatus,
  type KycDocument,
  type KycProfile,
} from "./kycSchemas";

export const KYC_DOCUMENTS_PAGE_SIZE = 20;

const documentsPageSchema = keysetPageSchema(kycDocumentSchema);

/**
 * KYC profile read (members:view; access-audited server-side). A 404
 * is a legitimate state — the member has no profile yet — surfaced to
 * the caller as the thrown ApiError to branch on.
 */
export async function fetchKycProfile(memberId: string): Promise<KycProfile> {
  const { data, error, response } = await api.GET("/members/{member_id}/profile", {
    params: { path: { member_id: memberId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return kycProfileSchema.parse(data);
}

/**
 * Create the member's KYC profile (members:create — a registration
 * wizard step). The profile payload is validated SERVER-SIDE against
 * the member row's type (the type is NEVER caller-supplied — a
 * wrong-type payload is a 422 naming field locations only).
 */
export async function createKycProfile(
  memberId: string,
  input: { category: string; profile: Record<string, unknown>; dpa_consent: boolean },
  idempotencyKey: string,
): Promise<KycProfile> {
  const { data, error, response } = await api.POST("/members/{member_id}/profile", {
    params: { path: { member_id: memberId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return kycProfileSchema.parse(data);
}

/**
 * Edit the member's KYC profile (members:edit): version-pinned
 * optimistic lock (stale version is a 409 with nothing changed).
 * dpa_consent accepts ONLY true — consent is granted, never withdrawn
 * through the API (0018 trigger is the DB backstop); the field is
 * sent only when this submission grants it.
 */
export async function updateKycProfile(
  memberId: string,
  input: {
    version: number;
    category?: string;
    profile?: Record<string, unknown>;
    dpa_consent?: true;
  },
  idempotencyKey: string,
): Promise<KycProfile> {
  const { data, error, response } = await api.PUT("/members/{member_id}/profile", {
    params: { path: { member_id: memberId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return kycProfileSchema.parse(data);
}

/** Checklist read (members:view; keyset, scalability; access-audited server-side per blocker f). */
export async function fetchKycDocumentsPage(
  memberId: string,
  cursor: string | null,
): Promise<KeysetPage<KycDocument>> {
  const { data, error, response } = await api.GET("/members/{member_id}/documents", {
    params: {
      path: { member_id: memberId },
      query: { cursor: cursor ?? undefined, limit: KYC_DOCUMENTS_PAGE_SIZE },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return documentsPageSchema.parse(data);
}

/**
 * Track a checklist document (members:create): creates the metadata
 * row in `pending`. Binary upload has NO contract (deferred behind
 * ADR-0003) — this is checklist METADATA only, stated on-screen.
 * A doc type outside the member type's checklist is a server 422.
 */
export async function createKycDocument(
  memberId: string,
  input: { doc_type: string; expires_at?: string | null },
  idempotencyKey: string,
): Promise<KycDocument> {
  const { data, error, response } = await api.POST("/members/{member_id}/documents", {
    params: { path: { member_id: memberId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return kycDocumentSchema.parse(data);
}

/**
 * Move a checklist document through its status machine and/or correct
 * its expiry (members:edit): version-pinned (409 on stale); an illegal
 * transition is a server 422 regardless of what the UI offered.
 * Exclude-unset semantics: pass `expires_at` ONLY when the
 * operator set it — omitted keeps the stored expiry, null clears it.
 */
export async function updateKycDocument(
  memberId: string,
  documentId: string,
  input: { version: number; status?: DocumentStatus; expires_at?: string | null },
  idempotencyKey: string,
): Promise<KycDocument> {
  const body: { version: number; status?: DocumentStatus; expires_at?: string | null } = {
    version: input.version,
  };
  if (input.status !== undefined) body.status = input.status;
  if (input.expires_at !== undefined) body.expires_at = input.expires_at;
  const { data, error, response } = await api.PUT("/members/{member_id}/documents/{document_id}", {
    params: { path: { member_id: memberId, document_id: documentId } },
    body,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return kycDocumentSchema.parse(data);
}
