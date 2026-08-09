/**
 * Tenant-settings API layer over the GENERATED
 * client. Settings ARE the money parameters: this API is their
 * single legitimate writer, every write is optimistic-locked
 * (`version`; a stale write surfaces as 409 — never a silent
 * overwrite) and carries a caller-supplied Idempotency-Key following
 * the stability/rotation contract (concurrency safety).
 *
 * PUT /settings semantics (mirrored from the backend contract): absent
 * keys keep their stored values; an explicit null CLEARS a key. Panels
 * submit exactly the fields they display (WYSIWYG per tab).
 */
import { toApiError } from "@genesis/api-client";
import { api } from "@/lib/api";
import { settingsSchema, type Settings } from "./schemas";

export async function fetchSettings(): Promise<Settings> {
  const { data, error, response } = await api.GET("/settings");
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return settingsSchema.parse(data);
}

/**
 * Panel-scoped partial update. Decimal fields travel as STRINGS
 * verbatim (blocker (a)); band lists travel exactly as the operator
 * entered them — ordering/contiguity is the server's verdict.
 */
export interface UpdateSettingsInput {
  version: number;
  deposit_interest_annual_rate_pct?: string | null;
  dividend_rate_pct?: string | null;
  deposit_rebate_rate_pct?: string | null;
  penalty_rate_pct_per_month?: string | null;
  penalty_grace_days?: number | null;
  penalty_charged_on?: "instalment_in_arrears" | "full_outstanding" | null;
  loan_interest_method?: "reducing_balance" | "flat" | null;
  loan_interest_basis?: "thirty_360" | "actual_365" | null;
  loan_rate_bands?: { min_amount: string; max_amount: string | null; rate_pct: string }[] | null;
  min_share_capital?: string | null;
  registration_fee?: string | null;
  min_monthly_contribution?: string | null;
  max_member_exposure?: string | null;
  dormancy_period_months?: number | null;
  financial_year_end_month?: number | null;
  exit_notice_period_days?: number | null;
  exit_fee?: string | null;
  committee_size?: number | null;
  committee_quorum?: number | null;
  approval_bands?: { authority: string; max_amount: string | null }[] | null;
}

export async function updateSettings(
  input: UpdateSettingsInput,
  idempotencyKey: string,
): Promise<Settings> {
  const { data, error, response } = await api.PUT("/settings", {
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return settingsSchema.parse(data);
}
