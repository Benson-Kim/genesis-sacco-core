import { z } from "zod";

/**
 * Shared response-boundary shapes — the SINGLE copy (reuse-first) of the
 * canonical money-string and ISO-timestamp assertions that previously
 * lived only in the exits and reports modules (boundary-validation drift across module generations).
 *
 * MONEY RULE: money NEVER becomes a number in this
 * client. These schemas assert the SHAPE of the server's decimal
 * STRINGS so that a corrupted or mis-serialised figure ("007.10",
 * "1e5", "abc") is REJECTED at the Zod boundary instead of flowing
 * verbatim into fmtKes/fmtAmount on a financial surface (fmtAmount
 * deliberately renders unexpected shapes verbatim — the boundary is
 * the only guard).
 *
 * The shapes below mirror the backend serialisation HONESTLY — they
 * are derived from `backend/src/genesis`, not guessed:
 *
 * 1. CANONICAL money (`moneySchema`): a `numeric(18,2)` COLUMN value
 *    or a `to_cents`-quantised computed value serialised via
 *    `str(Decimal)` (e.g. `_loan_out`/`_txn_out`/`_out` in
 *    api/loan_book.py, api/transactions.py, api/member_exits.py;
 *    domain/money.py `to_cents` is the only rounding). The wire value
 *    is ALWAYS digits with exactly two decimal places and no leading
 *    zeros beyond a lone 0.
 *
 * 2. SIGNED canonical money (`signedMoneySchema`): canonical shape
 *    with an optional leading '-' — ONLY for the fields whose backend
 *    contract documents a legitimate negative branch. At authoring
 *    that is exactly `net_payable` (migration 0010 deliberately ships
 *    no CHECK >= 0: the loan-exceeds-assets branch). Every CHECK(>=0)
 *    column REJECTS the sign via `moneySchema`.
 *
 * 3. AGGREGATE money (`aggregateMoneySchema`): a SQL aggregate
 *    serialised via `str(Decimal)` WITHOUT re-quantisation. The
 *    backend computes these as `COALESCE(SUM(...), 0)` (e.g.
 *    application/dashboard.py DEPOSIT_TOTAL_SQL, SHARE_TOTAL_SQL and
 *    GUARANTEE_TOTALS_SQL, application/loans.py portfolio_summary,
 *    application/guarantees.py live_pledged_total): with rows the sum
 *    keeps the column's two-place scale ("150.00"); with NO rows the
 *    COALESCE zero literal is a scale-0 numeric, so an empty aggregate
 *    legitimately serialises as "0". Shape: a bare "0" OR the
 *    canonical two-place decimal — nothing else. Asserting the strict
 *    canonical shape here would reject legitimate empty-tenant
 *    responses, so this laxer-by-one-value shape is the honest
 *    boundary for aggregate fields.
 *
 * 4. SIGNED aggregate money (`signedAggregateMoneySchema`): aggregate
 *    shape with an optional leading '-' — ONLY for aggregates the
 *    backend documents as possibly negative: the monthly flow series
 *    (application/dashboard.py MONTHLY_FLOWS_SQL — reversal rows
 *    subtract in the month they were POSTED, so a month holding only
 *    reversals nets negative) and `free_capacity`
 *    (dashboard.py GuarantorCapacity — an ADVISORY "balance less
 *    pledged" difference read without the P9 locks, negative when the
 *    balance dropped after pledging or no deposit account exists).
 *
 * 5. ISO timestamp (`isoTimestampSchema`): exactly what the backend's
 *    `datetime.isoformat()` emits (an earlier review lesson) — seconds
 *    always present, optional fractional seconds, optional `Z`/`±HH:MM`
 *    offset. These fields feed `fmtDateTime` on operator-facing
 *    surfaces; a garbage timestamp would render "Invalid Date" there,
 *    so it is REJECTED at the boundary instead.
 */

/** Canonical server money decimal shape: `str(Decimal)` of a
 * `numeric(18,2)` column / `to_cents` result — two places, no leading
 * zeros beyond a lone 0, no sign. */
export const MONEY_RE = /^(?:0|[1-9]\d*)\.\d{2}$/;

/** Canonical shape with the documented negative branch's sign. */
export const SIGNED_MONEY_RE = /^-?(?:0|[1-9]\d*)\.\d{2}$/;

/** SQL-aggregate money: canonical shape OR the bare "0" an empty
 * `COALESCE(SUM(...), 0)` serialises to. */
export const AGGREGATE_MONEY_RE = /^(?:0|(?:0|[1-9]\d*)\.\d{2})$/;

/** SQL-aggregate money that may legitimately net negative. */
export const SIGNED_AGGREGATE_MONEY_RE = /^(?:0|-?(?:0|[1-9]\d*)\.\d{2})$/;

/** `datetime.isoformat()` shape (seconds always present, optional
 * fractional seconds, optional offset). */
export const TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/;

/** Non-negative canonical server decimal (two-place scale). */
export const moneySchema = z.string().regex(MONEY_RE);

/** Canonical decimal keeping its sign — ONLY for fields whose backend
 * contract documents a negative branch (see module docblock). */
export const signedMoneySchema = z.string().regex(SIGNED_MONEY_RE);

/** Non-negative SQL-aggregate decimal (canonical shape or bare "0"). */
export const aggregateMoneySchema = z.string().regex(AGGREGATE_MONEY_RE);

/** SQL-aggregate decimal that may legitimately carry a '-'. */
export const signedAggregateMoneySchema = z.string().regex(SIGNED_AGGREGATE_MONEY_RE);

/** ISO-8601 datetime SHAPE for every field feeding fmtDateTime. */
export const isoTimestampSchema = z.string().regex(TIMESTAMP_RE);
