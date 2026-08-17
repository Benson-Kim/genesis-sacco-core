"use client";

import { useState, type ReactNode } from "react";
import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { ApiError } from "@genesis/api-client";
import { clearSession } from "@/modules/auth/session";

/**
 * A 401 anywhere means the session is gone (refresh rotation failed or was
 * revoked): clear it and return to the gate.
 */
function tearDownOn401(error: unknown): void {
  if (error instanceof ApiError && error.status === 401) {
    clearSession();
    if (typeof window !== "undefined") {
      // Code-owned flag only — nothing attacker-controlled enters the URL.
      window.location.assign("/login?reason=expired");
    }
  }
}

/**
 * TanStack Query is the only server-state mechanism (MASTER_PROMPT §2.3).
 * The 401 teardown covers BOTH caches (scaffold review finding S3: queries
 * alone left a revoked session stuck mid-mutation). Mutations never retry
 * (exactly one write attempt per submission — gate 1.4); queries retry once.
 */
export function Providers({ children }: Readonly<{ children: ReactNode }>) {
  const [client] = useState(
    () =>
      new QueryClient({
        queryCache: new QueryCache({
          onError: tearDownOn401,
        }),
        mutationCache: new MutationCache({
          onError: tearDownOn401,
        }),
        defaultOptions: {
          queries: { retry: 1, refetchOnWindowFocus: false },
          mutations: { retry: 0 },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
