/**
 * Least-disclosure operator messages (gate 1.6, ported from !25):
 * 401/403/409/422 are DISTINCT, 403 gives no capability-probing hint
 * beyond "Not permitted." + correlation id, and nothing echoes server
 * internals or data.
 */
import { ApiError } from "@genesis/api-client";
import { isConflict, operatorMessage } from "../errors";

test("messages are least-disclosure and distinct per status", () => {
  expect(operatorMessage(new ApiError(401, "unauthenticated", "c1")).title).toBe(
    "Session expired — sign in again.",
  );
  expect(operatorMessage(new ApiError(403, "forbidden", "c2"))).toEqual({
    title: "Not permitted.",
    correlationId: "c2",
  });
  expect(operatorMessage(new ApiError(409, "conflict", "c3")).title).toMatch(/reload before retrying/);
  expect(operatorMessage(new ApiError(422, "validation_error", "c4")).title).toMatch(/Validation failed/);
  expect(operatorMessage(new ApiError(429, "rate_limited", "c5")).title).toMatch(/Too many attempts/);

  // 409 and 422 must never collapse into each other (distinct handling).
  expect(operatorMessage(new ApiError(409, "conflict", "x")).title).not.toBe(
    operatorMessage(new ApiError(422, "validation_error", "x")).title,
  );
});

test("unknown failures degrade to a generic message with no echo", () => {
  const { title, correlationId } = operatorMessage(new Error("ECONNREFUSED 10.0.0.5"));
  expect(title).toBe("Network error — check connectivity and retry.");
  expect(correlationId).toBeNull();
  expect(title).not.toContain("10.0.0.5");
});

test("isConflict flags exactly the 409 class", () => {
  expect(isConflict(new ApiError(409, "conflict", null))).toBe(true);
  expect(isConflict(new ApiError(422, "validation_error", null))).toBe(false);
  expect(isConflict(new Error("boom"))).toBe(false);
});
