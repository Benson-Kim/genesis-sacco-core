/**
 * Loan-book API layer (module 4 — API) over the GENERATED
 * client.
 *
 * - Keyset pagination ONLY: opaque `cursor` echoed back verbatim; no
 *   offset/page parameters exist here (scalability, tested).
 * - Every mutation takes a caller-supplied Idempotency-Key following the
 *   stability/rotation contract (concurrency safety). Neither money write carries
 *   a version field: the server serialises disbursement under the P7
 *   atomic contract (an already-moved application stage surfaces as
 *   409) and repayments under the loan row lock. A 409 is rendered via
 *   the explicit reload-and-re-enter flow, never replayed.
 * - Ids travel as path parameters serialized by the generated client;
 *   tokens/PII never enter URLs (least disclosure, tested).
 * - MONEY (blocker (a)): the repayment amount is a decimal STRING on
 *   the wire; every response figure is a server-computed decimal string
 *   asserted by the Zod boundary (numbers are REJECTED).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  disbursementSchema,
  loanSchema,
  portfolioSummarySchema,
  repaymentResultSchema,
  scheduleSchema,
  settlementQuoteSchema,
  type CashChannel,
  type Disbursement,
  type Loan,
  type LoanClass,
  type LoanStatus,
  type PortfolioSummary,
  type RepaymentCreateInput,
  type RepaymentResult,
  type ScheduleRow,
  type SettlementQuote,
} from "./schemas";

export const LOANS_PAGE_SIZE = 20;

const loanPageSchema = keysetPageSchema(loanSchema);

export interface LoanListFilters {
  status: LoanStatus | "";
  classification: LoanClass | "";
}

export async function fetchLoansPage(
  filters: LoanListFilters,
  cursor: string | null,
): Promise<KeysetPage<Loan>> {
  const { data, error, response } = await api.GET("/loans", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: LOANS_PAGE_SIZE,
        status: filters.status === "" ? undefined : filters.status,
        classification: filters.classification === "" ? undefined : filters.classification,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return loanPageSchema.parse(data);
}

export async function fetchLoan(loanId: string): Promise<Loan> {
  const { data, error, response } = await api.GET("/loans/{loan_id}", {
    params: { path: { loan_id: loanId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return loanSchema.parse(data);
}

export async function fetchLoanSchedule(loanId: string): Promise<ScheduleRow[]> {
  const { data, error, response } = await api.GET("/loans/{loan_id}/schedule", {
    params: { path: { loan_id: loanId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return scheduleSchema.parse(data);
}

/** Early settlement quote as of TODAY (server-resolved; the contract
 * rejects future as-of dates — the UI sends none). */
export async function fetchSettlementQuote(loanId: string): Promise<SettlementQuote> {
  const { data, error, response } = await api.GET("/loans/{loan_id}/settlement-quote", {
    params: { path: { loan_id: loanId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return settlementQuoteSchema.parse(data);
}

/** The NPL, PAR-30 and provisioning aggregates — the SAME server model
 * that feeds the dashboard slice (never reconstructed from list pages). */
export async function fetchPortfolioSummary(): Promise<PortfolioSummary> {
  const { data, error, response } = await api.GET("/portfolio/summary");
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return portfolioSummarySchema.parse(data);
}

/**
 * Disburse a committee-APPROVED application via the P7 atomic contract
 * (loan_book:create). The body carries the cash channel ONLY — the
 * posting date and every money figure are resolved server-side
 * (extra="forbid" makes a caller-sent figure a 422). A stage that moved
 * underneath (already disbursed, rejected) surfaces as 409.
 */
export async function disburseApplication(
  applicationId: string,
  channel: CashChannel,
  idempotencyKey: string,
): Promise<Disbursement> {
  const { data, error, response } = await api.POST(
    "/applications/{application_id}/disburse",
    {
      params: { path: { application_id: applicationId } },
      body: { channel },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return disbursementSchema.parse(data);
}

/**
 * Record a repayment (transactions:create per the P4 matrix). The
 * amount travels as the typed decimal STRING; the server allocates it
 * (penalties, then interest, then principal) under the loan row lock
 * and returns the allocation verbatim.
 */
export async function recordRepayment(
  loanId: string,
  input: RepaymentCreateInput,
  idempotencyKey: string,
): Promise<RepaymentResult> {
  const { data, error, response } = await api.POST("/loans/{loan_id}/repayments", {
    params: { path: { loan_id: loanId } },
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return repaymentResultSchema.parse(data);
}
