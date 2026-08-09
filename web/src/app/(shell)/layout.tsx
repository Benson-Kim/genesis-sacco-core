"use client";

import type { ReactNode } from "react";
import { RequireAuth } from "@/modules/auth/components/RequireAuth";
import { AppShell } from "@/modules/layout/AppShell";

export default function ShellLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}
