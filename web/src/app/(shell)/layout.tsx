"use client";

import type { ReactNode } from "react";
import { RequireAuth } from "@/modules/auth/components/RequireAuth";
import { IdleLogoutGuard } from "@/modules/auth/components/IdleLogoutGuard";
import { AppShell } from "@/modules/layout/AppShell";

export default function ShellLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <IdleLogoutGuard />
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}
