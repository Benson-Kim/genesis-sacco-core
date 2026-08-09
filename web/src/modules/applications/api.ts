/**
 * Loan-applications / committee API layer (module 3 — P9 API) over
 * the GENERATED client.
 *
 * - Keyset pagination ONLY: opaque `cursor` echoed back verbatim; no
 *   offset/page parameters exist here (scalability, tested).
 * - Every mutation takes a caller-supplied Idempotency-Key following the
 *   stability/rotation contract (concurrency safety). Stage transitions send the
 *   `version` the record was loaded with — a stale move surfaces as 409
 *   (never a silent overwrite). Votes carry no version: the server
 *   serialises voters under the application row lock and the UNIQUE
 *   one-vote-per-voter constraint makes double voting impossible.
 * - Ids travel as path parameters serialized by the generated client;
 *   tokens/PII never enter URLs (least disclosure, tested).
 * - Pricing is server-resolved from the product: create sends only the
 *   member, product, amount, term and purpose.
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  applicationSchema,
  productsSchema,
  voteResultSchema,
  type Application,
  type ApplicationCreateInput,
  type ApplicationStage,
  type Product,
  type Vote,
  type VoteResult,
} from "./schemas";

export const APPLICATIONS_PAGE_SIZE = 20;

const applicationPageSchema = keysetPageSchema(applicationSchema);

export interface ApplicationListFilters {
  stage: ApplicationStage | "";
}

export async function fetchApplicationsPage(
  filters: ApplicationListFilters,
  cursor: string | null,
): Promise<KeysetPage<Application>> {
  const { data, error, response } = await api.GET("/applications", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: APPLICATIONS_PAGE_SIZE,
        stage: filters.stage === "" ? undefined : filters.stage,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return applicationPageSchema.parse(data);
}

export async function fetchApplication(applicationId: string): Promise<Application> {
  const { data, error, response } = await api.GET("/applications/{application_id}", {
    params: { path: { application_id: applicationId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return applicationSchema.parse(data);
}

/** Loan products — reference data for the intake form and name display. */
export async function fetchProducts(): Promise<Product[]> {
  const { data, error, response } = await api.GET("/products");
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return productsSchema.parse(data);
}

export async function createApplication(
  input: ApplicationCreateInput,
  idempotencyKey: string,
): Promise<Application> {
  const { data, error, response } = await api.POST("/applications", {
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return applicationSchema.parse(data);
}

/**
 * Officer stage moves. The API accepts appraisal | committee | rejected
 * only — APPROVED is produced exclusively by committee quorum and
 * DISBURSED exclusively by the P7 disbursement contract (loan-book
 * module). The `version` is the optimistic lock (409 ⇒ ConflictBanner).
 */
export type TransitionTarget = Extract<
  ApplicationStage,
  "appraisal" | "committee" | "rejected"
>;

export async function transitionApplication(
  applicationId: string,
  input: { target: TransitionTarget; version: number },
  idempotencyKey: string,
): Promise<Application> {
  const { data, error, response } = await api.POST(
    "/applications/{application_id}/transition",
    {
      params: { path: { application_id: applicationId } },
      body: input,
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return applicationSchema.parse(data);
}

/**
 * Committee vote. Quorum is resolved server-side AT VOTE TIME from
 * tenant configuration; one vote per voter is enforced by the DB UNIQUE
 * (a second vote surfaces as 409). Approve votes are additionally
 * capped server-side by the approval-authority bands.
 */
export async function voteOnApplication(
  applicationId: string,
  vote: Vote,
  idempotencyKey: string,
): Promise<VoteResult> {
  const { data, error, response } = await api.POST(
    "/applications/{application_id}/vote",
    {
      params: { path: { application_id: applicationId } },
      body: { vote },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return voteResultSchema.parse(data);
}
