/**
 * Kenya MSISDN blur-validation mirror (item 1). The SERVER is the
 * truth — backend/src/genesis/domain/members.py normalize_kenya_msisdn
 * normalizes on write and refuses invalid input with a sanitized 422;
 * this mirror exists only so the operator corrects the field
 * immediately on blur. Accepted spellings are exactly the four
 * maintainer-declared formats: 07XXXXXXXX / 01XXXXXXXX and their
 * E.164 twins +2547XXXXXXXX / +2541XXXXXXXX. No digit harvesting —
 * a typo never silently becomes a different number.
 */

const KENYA_MSISDN_LOCAL = /^0([17])(\d{8})$/;
const KENYA_MSISDN_E164 = /^\+254[17]\d{8}$/;

/** Normalize to E.164 (+254…), or null when the value is invalid. */
export function normalizeKenyaMsisdn(raw: string): string | null {
  const value = raw.trim();
  if (KENYA_MSISDN_E164.test(value)) return value;
  const match = KENYA_MSISDN_LOCAL.exec(value);
  if (match !== null) return `+254${match[1]}${match[2]}`;
  return null;
}

/** Blur message — shown on blur, cleared on correction. */
export const KENYA_PHONE_MESSAGE =
  "Enter a Kenya mobile number like 0712345678, 0110000000 or +254712345678.";
