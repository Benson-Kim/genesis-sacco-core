/**
 * Idempotency-Key stability/rotation contract (gate 1.4) and error-shape
 * parsing: 401/403/409/422 must stay DISTINCT and least-disclosure.
 * (Salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238.)
 */
import { idempotencyKeyFor, type IdempotencyKeySlot } from "../index";
import { ApiError, toApiError } from "../errors";

describe("idempotencyKeyFor", () => {
  function slot(): IdempotencyKeySlot {
    return { key: null, body: null };
  }

  test("stable while the logical submission is unchanged; rotates on change", () => {
    let n = 0;
    const fresh = () => `key-${(n += 1)}`;
    const s = slot();
    // Hand-computed oracle: same body twice => key-1 twice; new body => key-2.
    expect(idempotencyKeyFor(s, '{"a":1}', fresh)).toBe("key-1");
    expect(idempotencyKeyFor(s, '{"a":1}', fresh)).toBe("key-1");
    expect(idempotencyKeyFor(s, '{"a":2}', fresh)).toBe("key-2");
    // Returning to a previous body is a NEW logical submission (key-3):
    // the middleware must never replay the stale stored response.
    expect(idempotencyKeyFor(s, '{"a":1}', fresh)).toBe("key-3");
  });

  test("default generator produces unique UUIDs", () => {
    const a = idempotencyKeyFor(slot(), "x");
    const b = idempotencyKeyFor(slot(), "x");
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe("toApiError", () => {
  function response(status: number): Response {
    return { status } as Response;
  }

  test("app envelope parses into category + correlation id", () => {
    const err = toApiError(
      { category: "conflict", correlation_id: "corr-9" },
      response(409),
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.category).toBe("conflict");
    expect(err.correlationId).toBe("corr-9");
    expect(err.fields).toEqual([]);
  });

  test("FastAPI 422 detail parses into field errors without trusting shapes", () => {
    const err = toApiError(
      {
        detail: [
          { loc: ["body", "email"], msg: "value is not a valid email" },
          { loc: ["query", "limit"], msg: "too large" },
          { junk: true },
        ],
      },
      response(422),
    );
    expect(err.status).toBe(422);
    expect(err.category).toBe("validation_error");
    expect(err.fields).toEqual([
      { field: "email", message: "value is not a valid email" },
      // Canonical key (W56-5): the location head is stripped for EVERY
      // location, so body and query verdicts address the same form
      // field identically and form-errors matching cannot miss one.
      { field: "limit", message: "too large" },
      { field: "", message: "invalid value" },
    ]);
  });

  test("W56-5: every location head normalizes to the same canonical key; nested paths and head-named nested segments survive", () => {
    const err = toApiError(
      {
        detail: [
          { loc: ["body", "loan_rate_bands", 0, "rate_pct"], msg: "must be positive" },
          { loc: ["path", "member_id"], msg: "not a uuid" },
          { loc: ["header", "Idempotency-Key"], msg: "missing" },
          { loc: ["cookie", "session"], msg: "expired" },
          // Head stripping applies to POSITION 0 ONLY: a nested segment
          // that happens to be named like a head is part of the key.
          { loc: ["body", "payload", "query"], msg: "invalid" },
          // No recognised head: the path is already canonical.
          { loc: ["amount"], msg: "required" },
        ],
      },
      response(422),
    );
    expect(err.fields).toEqual([
      { field: "loan_rate_bands.0.rate_pct", message: "must be positive" },
      { field: "member_id", message: "not a uuid" },
      { field: "Idempotency-Key", message: "missing" },
      { field: "session", message: "expired" },
      { field: "payload.query", message: "invalid" },
      { field: "amount", message: "required" },
    ]);
  });

  test("garbage bodies degrade to a sanitized internal error — no echo", () => {
    const err = toApiError("<html>stack trace…</html>", response(500));
    expect(err.category).toBe("internal_error");
    expect(err.correlationId).toBeNull();
    expect(err.message).not.toContain("stack");
  });
});
