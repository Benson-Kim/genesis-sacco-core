/**
 * Per-request security headers (least disclosure): mint a CSP nonce, hand it to
 * Next.js via the request headers (so its inline bootstrap scripts are
 * nonced — the documented Next.js CSP pattern) and stamp the response
 * with the full header set built in lib/security-headers.ts.
 */
import { NextResponse, type NextRequest } from "next/server";
import {
  STATIC_SECURITY_HEADERS,
  buildContentSecurityPolicy,
} from "@/lib/security-headers";

export function middleware(request: NextRequest): NextResponse {
  const nonce = crypto.randomUUID();
  const csp = buildContentSecurityPolicy(nonce);

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  for (const header of STATIC_SECURITY_HEADERS) {
    response.headers.set(header.key, header.value);
  }
  return response;
}

export const config = {
  // Everything except Next's static assets (which carry no inline scripts).
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
