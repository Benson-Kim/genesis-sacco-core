/**
 * Guarantors API layer (module 5 — P9 pledging + release/substitution) over the GENERATED client.
 *
 * - The contract exposes NO guarantee list/read endpoint: GuaranteeOut
 *   exists only as a WRITE response (pledge/consent/release/substitute).
 *   Nothing here fabricates a read path; the screen's session panel and
 *   the honest-limitations register cover the gap.
 * - Every mutation takes a caller-supplied Idempotency-Key following the
 *   stability/rotation contract (concurrency safety). Consent, release and
 *   substitution send the `version` the record was witnessed with — a
 *   stale act surfaces as 409 (never a silent overwrite) and is NEVER
 *   replayed. The pledge is an unversioned CREATE anchored on the
 *   application record: the server serialises it under the application
 *   row lock + the guarantor's deposit-account lock, so a moved stage or
 *   exhausted capacity ALSO surfaces as 409 from exactly one attempt.
 * - Ids travel as path parameters serialized by the generated client;
 *   tokens/PII never enter URLs (least disclosure, tested).
 * - MONEY (blocker (a)): the pledge/substitution amount is a decimal
 *   STRING on the wire; every response figure is a server-computed
 *   decimal string asserted by the Zod boundary (numbers are REJECTED).
 *   Guarantor capacity is never computed here — the server enforces it
 *   under lock and the aggregate reports it.
 */
import { toApiError } from "@genesis/api-client";
import { api } from "@/lib/api";
import {
  guaranteeSchema,
  substitutionSchema,
  type Guarantee,
  type PledgeCreateInput,
  type SubstituteCreateInput,
  type Substitution,
} from "./schemas";

/**
 * Pledge a guarantee against an application (applications:edit). The
 * body carries the guarantor and the decimal-string amount ONLY; the
 * capacity check (deposits minus live pledges) runs SERVER-side under
 * the guarantor's deposit-account row lock — over-pledging surfaces as
 * 409, self-guarantee as a 400 (the UI additionally prevents offering
 * the borrower structurally).
 */
export async function pledgeGuarantee(
  applicationId: string,
  input: PledgeCreateInput,
  idempotencyKey: string,
): Promise<Guarantee> {
  const { data, error, response } = await api.POST(
    "/applications/{application_id}/guarantees",
    {
      params: { path: { application_id: applicationId } },
      body: input,
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return guaranteeSchema.parse(data);
}

/**
 * Record the staff-attested consent OVERRIDE: pledged -> active
 * (member_identity:approve — consent is the MEMBER principal's act on the /member surface; this staff path is an explicit attested override and MUST cite its evidence via `consent_reference`, written as an audited fact). The `version` pins the optimistic lock — a
 * consent racing another act surfaces as 409.
 */
export async function consentGuarantee(
  guaranteeId: string,
  version: number,
  consentReference: string,
  idempotencyKey: string,
): Promise<Guarantee> {
  const { data, error, response } = await api.POST("/guarantees/{guarantee_id}/consent", {
    params: { path: { guarantee_id: guaranteeId } },
    body: { version, consent_reference: consentReference },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return guaranteeSchema.parse(data);
}

/**
 * Release one guarantee per the rules (prototype "Release").
 * NO amounts, ever: the released amount comes from the guarantee row;
 * the body carries the optimistic-lock `version` only. An active
 * guarantee behind a DISBURSED loan is never bare-released (409) — the
 * substitution endpoint is the only exit, and the UI never offers what
 * the API forbids.
 */
export async function releaseGuarantee(
  guaranteeId: string,
  version: number,
  idempotencyKey: string,
): Promise<Guarantee> {
  const { data, error, response } = await api.POST("/guarantees/{guarantee_id}/release", {
    params: { path: { guarantee_id: guaranteeId } },
    body: { version },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return guaranteeSchema.parse(data);
}

/**
 * Atomic swap for a disbursed loan's collateral (applications:edit): releases the guarantee and creates the
 * replacement CONSENTED pledge in ONE server transaction. The
 * replacement amount is sent only when the operator typed one (a blank
 * lets the server derive it from the guarantee row); consent is a
 * first-class audited attestation citing its evidence.
 */
export async function substituteGuarantee(
  guaranteeId: string,
  version: number,
  input: SubstituteCreateInput,
  idempotencyKey: string,
): Promise<Substitution> {
  const { data, error, response } = await api.POST(
    "/guarantees/{guarantee_id}/substitute",
    {
      params: { path: { guarantee_id: guaranteeId } },
      body: {
        version,
        guarantor_member_id: input.guarantor_member_id,
        consent_reference: input.consent_reference,
        ...(input.amount !== null ? { amount: input.amount } : {}),
      },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return substitutionSchema.parse(data);
}
