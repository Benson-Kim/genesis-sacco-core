/**
 * Pure formatter oracles (HAND-COMPUTED, ported from !25
 * web/tests/format.test.mjs — never captured from the implementation).
 */
import { fmtDateTime, initials, isUuid, prettyJson, relTime } from "../format";

const HOSTILE = '<img src=x onerror=alert(1)>"?><script>x</script>';

test("prettyJson emits exact JSON text; hostile strings survive as data", () => {
  // Oracle: JSON.stringify semantics with 2-space indent, computed by hand.
  expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}');
  expect(prettyJson(HOSTILE)).toBe(JSON.stringify(HOSTILE));
  expect(prettyJson(null)).toBe("null");
  expect(prettyJson([1, "x"])).toBe('[\n  1,\n  "x"\n]');
});

test("prettyJson never throws on unserializable input", () => {
  const cyclic: Record<string, unknown> = {};
  cyclic.self = cyclic;
  expect(typeof prettyJson(cyclic)).toBe("string");
  expect(prettyJson(undefined)).toBe("undefined");
});

test("fmtDateTime / relTime fall back to the raw string for garbage", () => {
  expect(fmtDateTime(null)).toBe("—");
  expect(fmtDateTime("not-a-date")).toBe("not-a-date");
  expect(relTime(null)).toBe("never");
  expect(relTime("garbage")).toBe("garbage");
});

test("relTime buckets (hand-computed oracles)", () => {
  const now = new Date("2026-07-30T12:00:00Z");
  expect(relTime("2026-07-30T11:59:31Z", now)).toBe("just now"); // 29s
  expect(relTime("2026-07-30T11:58:00Z", now)).toBe("2m ago"); // 120s
  expect(relTime("2026-07-30T09:00:00Z", now)).toBe("3h ago"); // 3h
  expect(relTime("2026-07-25T12:00:00Z", now)).toBe("5d ago"); // 5d
  // Clock skew (future timestamp) clamps to zero, never negative.
  expect(relTime("2026-07-30T12:05:00Z", now)).toBe("just now");
});

test("initials are pure text, capped at two characters", () => {
  expect(initials("Jane Wanjiku Muthoni")).toBe("JW");
  // Hand-computed: parts "<img", "src=x", "onerror…" => "<" + "s", uppercased.
  expect(initials(HOSTILE)).toBe("<S");
  expect(initials("")).toBe("·");
});

test("isUuid accepts canonical UUIDs and rejects injection shapes", () => {
  expect(isUuid("fbbc9b6e-5178-4a55-8dad-913f56a93e27")).toBe(true);
  expect(isUuid("FBBC9B6E-5178-4A55-8DAD-913F56A93E27")).toBe(true);
  expect(isUuid("1 OR 1=1")).toBe(false);
  expect(isUuid("fbbc9b6e51784a558dad913f56a93e27")).toBe(false);
  expect(isUuid("fbbc9b6e-5178-4a55-8dad-913f56a93e27; DROP TABLE users")).toBe(false);
});
