import { fmtAmount, fmtDateTime, fmtKes, initials, isUuid, prettyJson, relTime } from "../format";

/**
 * Hand-computed oracles (MASTER_PROMPT §4 — never captured from the
 * implementation under test).
 */
describe("fmtAmount — string-only money rendering (P15 blocker (a))", () => {
    it("groups thousands by pure string transformation", () => {
        //   1234567.89 → "1,234,567.89": groups of three counted by hand from
        // the right of the integer part (1|234|567), fraction untouched.
        expect(fmtAmount("1234567.89")).toBe("1,234,567.89");
        // 1000 → "1,000" (one group boundary after the leading 1).
        expect(fmtAmount("1000")).toBe("1,000");
        // 999.5 → no grouping below four integer digits; fraction verbatim.
        expect(fmtAmount("999.5")).toBe("999.5");
        // 0.00 → zero stays un-grouped, trailing zeros PRESERVED verbatim
        // (a float round-trip would collapse them — proof no Number() ran).
        expect(fmtAmount("0.00")).toBe("0.00");
    });

    it("preserves signs and precision a float round-trip would destroy", () => {
        expect(fmtAmount("-2500000.00")).toBe("-2,500,000.00");
        // 18 digits exceed IEEE-754 exact integer range (2^53 ≈ 9.007e15):
        // correct output proves the value was never coerced to a number.
        expect(fmtAmount("123456789012345678.99")).toBe("123,456,789,012,345,678.99");
    });

    it("renders unexpected shapes verbatim instead of coercing", () => {
        expect(fmtAmount("n/a")).toBe("n/a");
        expect(fmtAmount("1,234")).toBe("1,234");
    });

    it("labels KES via fmtKes", () => {
        expect(fmtKes("54000.00")).toBe("KES 54,000.00");
    });
});

describe("fmtDateTime", () => {
    it("falls back to em-dash for empty and verbatim for unparseable", () => {
        expect(fmtDateTime(null)).toBe("—");
        expect(fmtDateTime(undefined)).toBe("—");
        expect(fmtDateTime("")).toBe("—");
        expect(fmtDateTime("not-a-date")).toBe("not-a-date");
    });

    it("formats a real ISO instant", () => {
        // Oracle: 2026-01-15T09:30:00Z rendered by en-KE contains day 15,
        // "Jan" and year 2026 (exact time-of-day depends on TZ, so assert
        // the date parts only).
        const rendered = fmtDateTime("2026-01-15T09:30:00Z");
        expect(rendered).toContain("15");
        expect(rendered).toContain("Jan");
        expect(rendered).toContain("2026");
    });
});

describe("initials", () => {
    it("uppercases the first letters of the first two words", () => {
        expect(initials("Amina Odhiambo")).toBe("AO");
        expect(initials("brian")).toBe("B");
        expect(initials("")).toBe("·");
    });
});

/**
 * Salvaged suites (duo/feature/p13-5-frontend-followthrough @ 198a238) —
 * hand-computed oracles, never captured from the implementation.
 */
describe("prettyJson — audit payloads as inert TEXT (the named XSS threat)", () => {
    const HOSTILE = '<img src=x onerror=alert(1)>"?><script>x</script>';

    it("emits exact JSON text; hostile strings survive as data", () => {
        // Oracle: JSON.stringify semantics with 2-space indent, by hand.
        expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}');
        expect(prettyJson(HOSTILE)).toBe(JSON.stringify(HOSTILE));
        expect(prettyJson(null)).toBe("null");
        expect(prettyJson([1, "x"])).toBe('[\n  1,\n  "x"\n]');
    });

    it("never throws on unserializable input", () => {
        const cyclic: Record<string, unknown> = {};
        cyclic.self = cyclic;
        expect(typeof prettyJson(cyclic)).toBe("string");
        expect(prettyJson(undefined)).toBe("undefined");
    });
});

describe("relTime — hand-computed buckets", () => {
    it("falls back to 'never' for empty and verbatim for garbage", () => {
        expect(relTime(null)).toBe("never");
        expect(relTime("garbage")).toBe("garbage");
    });

    it("buckets seconds/minutes/hours/days and clamps clock skew", () => {
        const now = new Date("2026-07-30T12:00:00Z");
        expect(relTime("2026-07-30T11:59:31Z", now)).toBe("just now"); // 29s
        expect(relTime("2026-07-30T11:58:00Z", now)).toBe("2m ago"); // 120s
        expect(relTime("2026-07-30T09:00:00Z", now)).toBe("3h ago"); // 3h
        expect(relTime("2026-07-25T12:00:00Z", now)).toBe("5d ago"); // 5d
        // Clock skew (future timestamp) clamps to zero, never negative.
        expect(relTime("2026-07-30T12:05:00Z", now)).toBe("just now");
    });
});

describe("isUuid — accepts canonical UUIDs, rejects injection shapes", () => {
    it("validates the canonical shape only", () => {
        expect(isUuid("fbbc9b6e-5178-4a55-8dad-913f56a93e27")).toBe(true);
        expect(isUuid("FBBC9B6E-5178-4A55-8DAD-913F56A93E27")).toBe(true);
        expect(isUuid("1 OR 1=1")).toBe(false);
        expect(isUuid("fbbc9b6e51784a558dad913f56a93e27")).toBe(false);
        expect(isUuid("fbbc9b6e-5178-4a55-8dad-913f56a93e27; DROP TABLE users")).toBe(false);
    });
});

