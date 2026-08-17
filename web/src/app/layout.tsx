import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";
import { FrameGuard } from "@/modules/auth/components/FrameGuard";

export const metadata: Metadata = {
  title: "Genesis Prestige Admin",
  description: "Genesis Prestige SACCO management — admin web",
};

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
