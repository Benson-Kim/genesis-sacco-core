/**
 * Gate (1.6 — web-admin security posture): the header set is strict and
 * stays strict. Each assertion is a named control: a loosened directive
 * (e.g. reintroducing 'unsafe-inline' for scripts, or dropping
 * frame-ancestors) fails this suite, not a review comment.
 */
import {
  STATIC_SECURITY_HEADERS,
  apiOrigin,
  buildContentSecurityPolicy,
} from "../security-headers";

function directives(csp: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const part of csp.split(";")) {
    const trimmed = part.trim();
    const space = trimmed.indexOf(" ");
    if (space === -1) {
      map.set(trimmed, "");
    } else {
      map.set(trimmed.slice(0, space), trimmed.slice(space + 1));
    }
  }
  return map;
}

describe("Content-Security-Policy", () => {
  const nonce = "test-nonce-123";
  const csp = buildContentSecurityPolicy(nonce);
  const policy = directives(csp);

  it("script-src is nonce-based with strict-dynamic — never unsafe-inline/eval", () => {
    const scriptSrc = policy.get("script-src");
    expect(scriptSrc).toBeDefined();
    expect(scriptSrc).toContain(`'nonce-${nonce}'`);
    expect(scriptSrc).toContain("'strict-dynamic'");
    expect(scriptSrc).not.toContain("unsafe-inline");
    expect(scriptSrc).not.toContain("unsafe-eval");
  });

  it("denies framing (clickjacking): frame-ancestors 'none'", () => {
    expect(policy.get("frame-ancestors")).toBe("'none'");
    expect(policy.get("frame-src")).toBe("'none'");
    expect(policy.get("object-src")).toBe("'none'");
  });

  it("locks default/base/form to self", () => {
    expect(policy.get("default-src")).toBe("'self'");
    expect(policy.get("base-uri")).toBe("'self'");
    expect(policy.get("form-action")).toBe("'self'");
  });

  it("connect-src allows only self plus the API origin — no third parties", () => {
    expect(policy.get("connect-src")).toBe(`'self' ${apiOrigin()}`);
  });

  it("the documented Next.js exception is confined to styles", () => {
    // Inline styles cannot execute script or exfiltrate tokens; inline
    // scripts stay nonce-gated. This is the honest, documented trade-off.
    expect(policy.get("style-src")).toBe("'self' 'unsafe-inline'");
  });
});

describe("static security headers", () => {
  const byKey = new Map(STATIC_SECURITY_HEADERS.map((h) => [h.key, h.value]));

  it("X-Frame-Options DENY (defence in depth with frame-ancestors)", () => {
    expect(byKey.get("X-Frame-Options")).toBe("DENY");
  });

  it("nosniff", () => {
    expect(byKey.get("X-Content-Type-Options")).toBe("nosniff");
  });

  it("Referrer-Policy strict-origin-when-cross-origin", () => {
    expect(byKey.get("Referrer-Policy")).toBe("strict-origin-when-cross-origin");
  });

  it("Permissions-Policy disables every powerful feature", () => {
    const value = byKey.get("Permissions-Policy");
    expect(value).toBeDefined();
    for (const feature of [
      "accelerometer",
      "camera",
      "geolocation",
      "gyroscope",
      "magnetometer",
      "microphone",
      "payment",
      "usb",
    ]) {
      expect(value).toContain(`${feature}=()`);
    }
  });
});

describe("next.config applies the static header set to every route", () => {
  it("headers() covers /:path* with the same header list", async () => {
    // next.config.ts imports the same STATIC_SECURITY_HEADERS constant,
    // so asserting the wiring here keeps a single source of truth.
    const { default: nextConfig } = await import("../../../next.config");
    expect(nextConfig.headers).toBeDefined();
    const rules = await nextConfig.headers!();
    expect(rules).toHaveLength(1);
    expect(rules[0]?.source).toBe("/:path*");
    expect(rules[0]?.headers).toEqual(
      STATIC_SECURITY_HEADERS.map((header) => ({ ...header })),
    );
  });
});
