/**
 * Accept/reject matrix for the SHARED boundary shapes (lib/schemas.ts —
 * issue #30 findings A2/S2). Oracles are HAND-COMPUTED from the backend
 * serialisation (MASTER_PROMPT §4):
 * - `str(Decimal)` of a numeric(18,2) column / to_cents result is
 *   ALWAYS two decimal places with no leading zeros beyond a lone 0.
 * - `COALESCE(SUM(...), 0)` over an EMPTY set serialises as the bare
 *   "0" (the SQL zero literal is a scale-0 numeric); with rows it
 *   keeps the two-place scale.
 * - `datetime.isoformat()` always carries seconds; fractional seconds
 *   and the offset are optional.
 *
 * Falsifiability: every matrix below contains at least one ACCEPT and
 * one REJECT per schema — a schema loosened to plain z.string() fails
 * the reject rows; one tightened past the real serialisation fails the
 * accept rows.
 */
import {
  aggregateMoneySchema,
  isoTimestampSchema,
  moneySchema,
  signedAggregateMoneySchema,
  signedMoneySchema,
} from "../schemas";

/** Canonical column/to_cents shapes the backend actually emits. */
const CANONICAL_MONEY = ["0.00", "0.10", "25000.10", "134500.20", "999999999999999.99"];

/** None of these is a str(Decimal) of a numeric(18,2) value. */
const GARBAGE_MONEY = [
  "abc",
  "1e5",
  "007.10", // leading zeros beyond a lone 0
  "25000.1", // wrong scale — the server always emits two places
  "25000.100",
  "25000", // missing scale on a COLUMN value
  "25,000.10", // grouping is fmtKes's job, never the wire's
  " 25000.10",
  "25000.10 ",
  "NaN",
  "Infinity",
  "0x10",
  ".10",
  "+1.00",
  "",
];

test("moneySchema: canonical non-negative column shapes accepted; garbage and signs rejected", () => {
  for (const value of CANONICAL_MONEY) {
    expect(moneySchema.safeParse(value).success).toBe(true);
  }
  for (const value of GARBAGE_MONEY) {
    expect(moneySchema.safeParse(value).success).toBe(false);
  }
  // A '-' is a contract violation on every CHECK (>= 0) column.
  expect(moneySchema.safeParse("-1.00").success).toBe(false);
  expect(moneySchema.safeParse("-0.00").success).toBe(false);
  // Aggregate zero is NOT a column value — columns always carry scale.
  expect(moneySchema.safeParse("0").success).toBe(false);
});

test("signedMoneySchema: the documented negative branch keeps its sign; garbage still rejected", () => {
  for (const value of CANONICAL_MONEY) {
    expect(signedMoneySchema.safeParse(value).success).toBe(true);
  }
  expect(signedMoneySchema.safeParse("-4000.00").success).toBe(true);
  expect(signedMoneySchema.safeParse("-0.01").success).toBe(true);
  for (const value of GARBAGE_MONEY) {
    expect(signedMoneySchema.safeParse(value).success).toBe(false);
  }
  expect(signedMoneySchema.safeParse("-007.10").success).toBe(false);
  expect(signedMoneySchema.safeParse("--1.00").success).toBe(false);
  expect(signedMoneySchema.safeParse("-").success).toBe(false);
});

test("aggregateMoneySchema: canonical shapes plus the empty-aggregate bare '0'; everything else rejected", () => {
  for (const value of CANONICAL_MONEY) {
    expect(aggregateMoneySchema.safeParse(value).success).toBe(true);
  }
  // COALESCE(SUM(...), 0) over an empty set — the one extra shape.
  expect(aggregateMoneySchema.safeParse("0").success).toBe(true);
  for (const value of GARBAGE_MONEY) {
    expect(aggregateMoneySchema.safeParse(value).success).toBe(false);
  }
  // The bare-zero allowance never widens to other bare integers or
  // signs: "150" is not an aggregate the backend can emit (a non-empty
  // SUM keeps the two-place scale).
  expect(aggregateMoneySchema.safeParse("150").success).toBe(false);
  expect(aggregateMoneySchema.safeParse("00").success).toBe(false);
  expect(aggregateMoneySchema.safeParse("-1.00").success).toBe(false);
  expect(aggregateMoneySchema.safeParse("-0").success).toBe(false);
});

test("signedAggregateMoneySchema: negative nets accepted (reversal-only months, advisory free capacity); garbage rejected", () => {
  for (const value of [...CANONICAL_MONEY, "0", "-100.00", "-0.01"]) {
    expect(signedAggregateMoneySchema.safeParse(value).success).toBe(true);
  }
  for (const value of GARBAGE_MONEY) {
    expect(signedAggregateMoneySchema.safeParse(value).success).toBe(false);
  }
  // A signed BARE integer is not a shape the backend can emit: the
  // negative branch always comes from two-place-scale arithmetic.
  expect(signedAggregateMoneySchema.safeParse("-0").success).toBe(false);
  expect(signedAggregateMoneySchema.safeParse("-150").success).toBe(false);
});

test("isoTimestampSchema: datetime.isoformat() shapes accepted; garbage rejected ('Invalid Date' can never render)", () => {
  const accepted = [
    "2026-08-01T09:00:00",
    "2026-08-01T09:00:00Z",
    "2026-08-01T09:00:00+00:00",
    "2026-08-01T09:00:00-03:00",
    "2026-08-01T09:00:00.123456+00:00",
  ];
  for (const value of accepted) {
    expect(isoTimestampSchema.safeParse(value).success).toBe(true);
  }
  const rejected = [
    "yesterday-ish",
    "2026-08-01", // a bare DATE is not a timestamp
    "2026-08-01 09:00:00", // space separator — isoformat emits 'T'
    "2026-08-01T09:00", // seconds always present in isoformat output
    "2026-08-01T09:00:00+0000",
    "01/08/2026 09:00",
    "",
  ];
  for (const value of rejected) {
    expect(isoTimestampSchema.safeParse(value).success).toBe(false);
  }
});
