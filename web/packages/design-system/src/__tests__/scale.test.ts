/**
 * Gate: the structural scale obeys the binding design rules of issue #8
 * (P14) — 8-point grid, nested corner-radius math, unitless proportional
 * line-heights, rem-anchored clamp() fluid type, ch line-length caps,
 * <400ms micro-interaction budget, 44–48px touch targets. Every oracle
 * below is hand-computed from the issue's formulas, never captured from
 * the implementation.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  DURATION_BUDGET_MS,
  MICRO_RADIUS_MAX,
  MICRO_RADIUS_MIN,
  duration,
  fontSize,
  lineHeight,
  measure,
  nestedInnerRadius,
  nestedOuterRadius,
  radius,
  spacing,
  touchTarget,
} from "../scale";

describe("8-point grid (spacing)", () => {
  it("every spacing step is a multiple of 4px and the 8px anchor exists", () => {
    for (const value of Object.values(spacing)) {
      expect(value % 4).toBe(0);
      expect(value).toBeGreaterThan(0);
    }
    expect(Object.values(spacing) as readonly number[]).toContain(8);
  });
});

describe("nested corner-radius math (issue #8 formulas)", () => {
  it("outer = inner + padding: 16px inner in 8px padding needs a 24px outer", () => {
    expect(nestedOuterRadius(16, 8)).toBe(24);
  });

  it("inner = outer − padding: 20px outer with 8px padding gives 12px inner", () => {
    expect(nestedInnerRadius(20, 8)).toBe(12);
  });

  it("when the math hits zero or below, a 2–4px micro-radius applies", () => {
    // 10 − 12 = −2 → micro-radius, never a dead-square 0.
    const collapsed = nestedInnerRadius(10, 12);
    expect(collapsed).toBeGreaterThanOrEqual(MICRO_RADIUS_MIN);
    expect(collapsed).toBeLessThanOrEqual(MICRO_RADIUS_MAX);
    // Exactly zero (padding == outer radius) also collapses to micro.
    const zero = nestedInnerRadius(16, 16);
    expect(zero).toBeGreaterThanOrEqual(MICRO_RADIUS_MIN);
    expect(zero).toBeLessThanOrEqual(MICRO_RADIUS_MAX);
  });

  it("radius.micro itself sits inside the 2–4px band", () => {
    expect(radius.micro).toBeGreaterThanOrEqual(MICRO_RADIUS_MIN);
    expect(radius.micro).toBeLessThanOrEqual(MICRO_RADIUS_MAX);
  });
});

describe("proportional line-heights", () => {
  it("body is a unitless multiplier in the 1.4–1.6 band", () => {
    expect(typeof lineHeight.body).toBe("number");
    expect(lineHeight.body).toBeGreaterThanOrEqual(1.4);
    expect(lineHeight.body).toBeLessThanOrEqual(1.6);
  });

  it("headings are tighter, in the 1.1–1.2 band", () => {
    expect(typeof lineHeight.heading).toBe("number");
    expect(lineHeight.heading).toBeGreaterThanOrEqual(1.1);
    expect(lineHeight.heading).toBeLessThanOrEqual(1.2);
  });
});

describe("fluid type — clamp() anchored in rem", () => {
  const CLAMP =
    /^clamp\((\d*\.?\d+)rem, (\d*\.?\d+)rem \+ (\d*\.?\d+)vw, (\d*\.?\d+)rem\)$/;

  it.each(Object.entries(fontSize))("%s is rem-anchored, never px", (_name, value) => {
    expect(value).toMatch(CLAMP);
    expect(value).not.toContain("px");
  });

  it.each(Object.entries(fontSize))(
    "%s slope follows (max−min)/(max-vw−min-vw) across the 480→1280px band",
    (_name, value) => {
      const match = CLAMP.exec(value);
      expect(match).not.toBeNull();
      const [, minRem, interceptRem, slopeVw, maxRem] = match as unknown as [
        string,
        string,
        string,
        string,
        string,
      ];
      const min = parseFloat(minRem) * 16;
      const max = parseFloat(maxRem) * 16;
      const intercept = parseFloat(interceptRem) * 16;
      const slope = parseFloat(slopeVw) / 100;
      expect(max).toBeGreaterThan(min);
      // Hand-computed anchors: preferred value equals min at 480px wide
      // and max at 1280px wide (e.g. heading: (24−20)/(1280−480)=0.005
      // → 0.5vw; 1.1rem·16 + 0.005·480 = 17.6 + 2.4 = 20px).
      expect(intercept + slope * 480).toBeCloseTo(min, 5);
      expect(intercept + slope * 1280).toBeCloseTo(max, 5);
    },
  );
});

describe("line-length caps (ch)", () => {
  it("body measure is ch-based inside the 45–75ch readable band", () => {
    for (const value of Object.values(measure)) {
      const match = /^(\d+)ch$/.exec(value);
      expect(match).not.toBeNull();
      const chars = Number((match as RegExpExecArray)[1]);
      expect(chars).toBeGreaterThanOrEqual(45);
      expect(chars).toBeLessThanOrEqual(75);
    }
    expect(parseInt(measure.body, 10)).toBeLessThanOrEqual(parseInt(measure.max, 10));
  });
});

describe("micro-interaction budget", () => {
  it("every duration token is strictly under the 400ms budget", () => {
    for (const value of Object.values(duration)) {
      expect(value).toBeLessThan(DURATION_BUDGET_MS);
      expect(value).toBeGreaterThan(0);
    }
  });
});

describe("touch targets", () => {
  it("interactive minimums sit in the 44–48px band", () => {
    expect(touchTarget.min).toBeGreaterThanOrEqual(44);
    expect(touchTarget.comfortable).toBeGreaterThanOrEqual(touchTarget.min);
    expect(touchTarget.comfortable).toBeLessThanOrEqual(48);
  });
});

describe("scale.css stays in sync with scale.ts", () => {
  function parseRootVariables(css: string): Record<string, string> {
    const rootMatch = /:root\s*\{([\s\S]*?)\n\}/.exec(css);
    if (!rootMatch || rootMatch[1] === undefined) {
      throw new Error("no :root block found");
    }
    const variables: Record<string, string> = {};
    const varPattern = /--([a-z-]+)\s*:\s*([^;]+);/g;
    let match: RegExpExecArray | null;
    while ((match = varPattern.exec(rootMatch[1])) !== null) {
      const [, name, value] = match;
      if (name !== undefined && value !== undefined) {
        variables[name] = value.trim();
      }
    }
    return variables;
  }

  it("every TS token has an identical CSS custom property", () => {
    const css = readFileSync(join(__dirname, "..", "scale.css"), "utf8");
    const cssVars = parseRootVariables(css);
    const expected: Record<string, string> = {
      "space-xxs": `${spacing.xxs}px`,
      "space-xs": `${spacing.xs}px`,
      "space-sm": `${spacing.sm}px`,
      "space-md": `${spacing.md}px`,
      "space-lg": `${spacing.lg}px`,
      "space-xl": `${spacing.xl}px`,
      "space-xxl": `${spacing.xxl}px`,
      "radius-micro": `${radius.micro}px`,
      "radius-xs": `${radius.xs}px`,
      "radius-sm": `${radius.sm}px`,
      "radius-md": `${radius.md}px`,
      "radius-lg": `${radius.lg}px`,
      "radius-xl": `${radius.xl}px`,
      "radius-pill": `${radius.pill}px`,
      "leading-heading": `${lineHeight.heading}`,
      "leading-body": `${lineHeight.body}`,
      "fs-meta": fontSize.meta,
      "fs-body": fontSize.body,
      "fs-title": fontSize.title,
      "fs-heading": fontSize.heading,
      "measure-body": measure.body,
      "measure-max": measure.max,
      "duration-instant": `${duration.instant}ms`,
      "duration-fast": `${duration.fast}ms`,
      "duration-base": `${duration.base}ms`,
      "duration-slow": `${duration.slow}ms`,
      "touch-target-min": `${touchTarget.min}px`,
      "touch-target-comfortable": `${touchTarget.comfortable}px`,
    };
    expect(cssVars).toEqual(expected);
  });
});
