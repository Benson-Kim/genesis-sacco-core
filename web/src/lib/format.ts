/**
  * Pure formatting helpers (no DOM, no I/O — testable), salvaged from the
  *  frontend branch (duo/feature/p13-5-frontend-followthrough).
  *
  * MONEY RULE (the money rule, the house doctrine): amounts arrive from the
  * API as decimal STRINGS and are rendered by string manipulation only.
  * There is NO parseFloat/Number conversion and NO arithmetic on money
  * values anywhere in the client — the ledger is the single source of
  * truth and every derived figure is server-computed. The no-money-math
  * grep gate (src/__tests__/no-money-math.test.ts) enforces this.
  */

const AMOUNT_RE = /^(-?)(\d+)(?:\.(\d+))?$/;

/**
 * Group an API decimal string with thousands separators — pure string
 * transformation, no numeric conversion. Unexpected shapes render
 * verbatim (as inert React text) rather than being coerced.
 */
export function fmtAmount(value: string): string {
    const match = AMOUNT_RE.exec(value.trim());
    if (match === null) return value;
    const sign = match[1] ?? "";
    const integer = match[2];
    const fraction = match[3];
    if (integer === undefined) return value;
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return `${sign}${grouped}${fraction !== undefined ? `.${fraction}` : ""}`;
}

/** KES-labelled amount for stat cards and table cells. */
export function fmtKes(value: string): string {
    return `KES ${fmtAmount(value)}`;
}

/**
 * Human-readable timestamp from an ISO string; falls back to the raw
 * string (rendered as inert text by React) when unparseable.
 */
export function fmtDateTime(iso: string | null | undefined): string {
    if (iso === null || iso === undefined || iso === "") return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return new Intl.DateTimeFormat("en-KE", {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    }).format(d);
}

/** Uppercase initials for the prototype avatar chip (pure text). */
export function initials(name: string): string {
    const parts = name.split(" ").filter((part) => part !== "");
    const chars = parts
        .map((part) => part.charAt(0))
        .join("")
        .slice(0, 2)
        .toUpperCase();
    return chars === "" ? "·" : chars;
}

/**
 * Pretty-print an API-provided JSON value as plain TEXT (audit payload
 * handling — the named XSS threat): payloads render exactly as the API
 * returned them, with no client-side reconstruction of redacted fields
 * and no interpretation of payload contents.
 * (Salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238.)
 */
export function prettyJson(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2) ?? String(value);
    } catch {
        return String(value);
    }
}

/** Compact relative time ("2m ago") for last-active columns. */
export function relTime(iso: string | null | undefined, now: Date = new Date()): string {
    if (iso === null || iso === undefined || iso === "") return "never";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const sec = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 1000));
    if (sec < 60) return "just now";
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hrs = Math.floor(min / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Client-side UUID shape check (the server validates regardless). */
export function isUuid(value: string): boolean {
    return UUID_RE.test(value);
}
