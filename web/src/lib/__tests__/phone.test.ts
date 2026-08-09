/**
 * #35 item 1 — client mirror of the server's Kenya MSISDN rule
 * (backend domain normalize_kenya_msisdn). Hand-computed oracles are
 * BYTE-IDENTICAL to backend/tests/test_members_domain.py so the two
 * mirrors can never drift silently: every accepted spelling of the
 * same subscriber converges to ONE E.164 string; everything else is
 * null (no digit harvesting).
 */
import { normalizeKenyaMsisdn } from "../phone";

test("every accepted format converges to the same E.164 string", () => {
  expect(normalizeKenyaMsisdn("0712345678")).toBe("+254712345678");
  expect(normalizeKenyaMsisdn("+254712345678")).toBe("+254712345678");
  expect(normalizeKenyaMsisdn("0110000000")).toBe("+254110000000");
  expect(normalizeKenyaMsisdn("+254110000000")).toBe("+254110000000");
  // Surrounding whitespace is a hand-typing courtesy, nothing more.
  expect(normalizeKenyaMsisdn("  0712345678 ")).toBe("+254712345678");
});

test("everything else is rejected — no digit harvesting from free text", () => {
  const rejected = [
    "",
    "0712345 678", // interior whitespace is NOT harvested
    "071234567", // too short
    "07123456789", // too long
    "0212345678", // 02 — not a Kenya mobile prefix
    "0812345678", // 08 — not accepted
    "254712345678", // missing the +
    "+255712345678", // Tanzania country code
    "+25471234567", // +254 but 8 subscriber digits
    "07-1234-5678", // punctuation is not harvested
    "0712345678x", // trailing junk
    "a0712345678", // leading junk
    "+254712345678 ext 2", // trailing text
  ];
  for (const value of rejected) {
    expect(normalizeKenyaMsisdn(value)).toBeNull();
  }
});
