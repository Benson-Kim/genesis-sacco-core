/**
 * Design tokens — extracted VERBATIM from the `:root` CSS variables of the
 * canonical prototype (`genesis_prestige_app.html`). Do not add ad-hoc
 * colors in views (the house doctrine reuse-first); the test suite asserts these
 * values byte-for-byte against the prototype.
 */
export const colorTokens = {
  navy: "#0F2C6B",
  navyMid: "#1E4FB0",
  steel: "#3E6FD0",
  navySoft: "#E6EDFB",
  gold: "#2E90FA",
  goldSoft: "#DCEBFF",
  ink: "#131A2B",
  sub: "#5C6579",
  line: "#E1E4EC",
  bg: "#F3F4F7",
  panel: "#EBEEF3",
  card: "#fff",
  emerald: "#1B7A54",
  emeraldSoft: "#DCEFE6",
  brick: "#B23A2E",
  brickSoft: "#F6E1DD",
  orange: "#C2691C",
  orangeSoft: "#F7E7D4",
  loss: "#6E241B",
} as const;

export type ColorToken = keyof typeof colorTokens;

/** Prototype body font stack. */
export const fontFamily = "-apple-system,'Segoe UI',Roboto,system-ui,sans-serif";

/** Reference a token as a CSS custom property (defined in tokens.css). */
export function cssVar(token: ColorToken): string {
  return `var(--${token})`;
}
