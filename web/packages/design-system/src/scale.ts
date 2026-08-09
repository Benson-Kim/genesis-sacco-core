/**
 * Layout / type / motion scale tokens — governed by the binding design
 * rules of. The color palette stays VERBATIM in tokens.ts;
 * everything here is the structural system those rules mandate:
 *
 * - 8-point grid: every spacing step is a multiple of 4px, anchored on 8px.
 * - Nested corner-radius math: inner = outer − padding (and the reverse),
 *   with a 2–4px micro-radius floor when the math hits zero.
 * - Unitless proportional line-heights (body 1.4–1.6, headings 1.1–1.2).
 * - Fluid type via clamp() anchored in rem (never px) so browser font-size
 *   preferences keep working.
 * - ch-based line-length caps (45–75ch) for readable body copy.
 * - Micro-interaction duration budget strictly < 400ms, with the
 *   prefers-reduced-motion fallback applied globally (globals.css).
 * - 44–48px minimum touch targets.
 *
 * The scale test suite enforces each rule mathematically — an off-grid or
 * over-budget token is a red pipeline, not a review comment (reuse-first).
 */

/** 8-point grid (4px half-steps allowed by the grid discipline). */
export const spacing = {
  xxs: 4,
  xs: 8,
  sm: 12,
  md: 16,
  lg: 24,
  xl: 32,
  xxl: 48,
} as const;

export type SpacingToken = keyof typeof spacing;

/**
 * Corner radii. `pill` is the prototype's fully-rounded 999px convention.
 * Nested elements must NOT reuse these ad hoc — derive the inner radius
 * with nestedInnerRadius() so concentric curves stay harmonious.
 */
export const radius = {
  micro: 3,
  xs: 4,
  sm: 8,
  md: 10,
  lg: 16,
  xl: 20,
  pill: 999,
} as const;

export type RadiusToken = keyof typeof radius;

/** Micro-radius bounds for the "when the math hits zero" rule. */
export const MICRO_RADIUS_MIN = 2;
export const MICRO_RADIUS_MAX = 4;

/**
 * Nested corner-radius math: the inner element's radius is the
 * outer radius minus the padding between them. When that reaches zero or
 * below, fall back to a soft micro-radius instead of a dead-square corner.
 *
 * nestedInnerRadius(24, 8) === 16; nestedInnerRadius(20, 8) === 12;
 * nestedInnerRadius(10, 12) === radius.micro (3).
 */
export function nestedInnerRadius(outerRadius: number, padding: number): number {
  const inner = outerRadius - padding;
  if (inner < MICRO_RADIUS_MIN) {
    return radius.micro;
  }
  return inner;
}

/** Reverse formula: a container wrapping an inner radius with padding. */
export function nestedOuterRadius(innerRadius: number, padding: number): number {
  return innerRadius + padding;
}

/**
 * Unitless proportional line-heights: body 1.4–1.6 so text
 * scales cleanly on resize; headings tighter at 1.1–1.2 so multi-line
 * titles do not disconnect.
 */
export const lineHeight = {
  heading: 1.15,
  body: 1.5,
} as const;

/**
 * Fluid type steps: clamp() anchored in rem (accessibility — px anchors
 * would ignore the user's browser font-size preference). The vw slope for
 * each step derives from the issue- formula
 * (maxRem − minRem) / (maxViewport − minViewport), anchored 480→1280px.
 */
export const fontSize = {
  /** Small meta text (prototype 11–12px band). */
  meta: "clamp(0.6875rem, 0.65rem + 0.125vw, 0.75rem)",
  /** Body / controls (prototype 13–14px band). */
  body: "clamp(0.8125rem, 0.775rem + 0.125vw, 0.875rem)",
  /** Section titles (prototype 16–18px band). */
  title: "clamp(1rem, 0.925rem + 0.25vw, 1.125rem)",
  /** Page headings (prototype 20–24px band). */
  heading: "clamp(1.25rem, 1.1rem + 0.5vw, 1.5rem)",
} as const;

export type FontSizeToken = keyof typeof fontSize;

/**
 * Line-length caps in ch (keep body copy roughly 50–85 characters per line; clamp the measure between 45 and 75ch).
 */
export const measure = {
  body: "65ch",
  max: "75ch",
} as const;

/**
 * Micro-interaction durations in ms — the issue- budget is strictly
 * < 400ms, single-movement physics. globals.css collapses these under
 * prefers-reduced-motion.
 */
export const duration = {
  instant: 80,
  fast: 120,
  base: 200,
  slow: 320,
} as const;

export const DURATION_BUDGET_MS = 400;

/** Minimum interactive target sizes in px (44–48px). */
export const touchTarget = {
  min: 44,
  comfortable: 48,
} as const;
