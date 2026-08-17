/**
 * Pure formatting helpers (no DOM, no I/O — testable), ported from !25.
 *
 * Audit payload handling (the named XSS threat): payloads are rendered as
 * pretty-printed JSON TEXT exactly as the API returned them. There is no
 * client-side reconstruction of redacted fields and no interpretation of
 * payload contents.
 */

/** Pretty-print an API-provided JSON value as plain text. */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
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

/** Uppercase initials for the prototype avatar chip (pure text). */
export function initials(name: string): string {
  const parts = name.split(" ").filter((part) => part !== "");
  const chars = parts.map((part) => part.charAt(0)).join("").slice(0, 2).toUpperCase();
  return chars === "" ? "·" : chars;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Client-side UUID shape check (server validates regardless). */
export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}
