import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";
import { FrameGuard } from "@/modules/auth/components/FrameGuard";

export const metadata: Metadata = {
  title: "Genesis Prestige Admin",
  description: "Genesis Prestige SACCO management — admin web",
};

/**
 * Force per-request rendering so every response carries the middleware's
 * CSP nonce on Next's inline scripts (a statically prerendered page would
 * embed un-nonced inline scripts and be blocked by the strict CSP). An
 * authenticated admin tool has no cacheable public pages to lose.
 */
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <FrameGuard>
          <Providers>{children}</Providers>
        </FrameGuard>
      </body>
    </html>
  );
}
