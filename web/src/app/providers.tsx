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
import { clearSessionScopedStores } from "@/modules/auth/sessionScopedStores";

/**
 * A 401 anywhere means the session is gone (refresh rotation failed or was
 * revoked): clear it and return to the gate.
 */
function tearDownOn401(error: unknown): void {
  if (error instanceof ApiError && error.status === 401) {
    clearSession();
    // Every per-tab witnessed registry dies WITH the session (W58-2,
    // the F2 class): an in-tab operator switch inherits nothing
    // from the previous operator's identity — no witnessed
    // attributions, no armed affordances.
    clearSessionScopedStores();
    if (typeof window !== "undefined") {
      // Code-owned flag only — nothing attacker-controlled enters the URL.
      window.location.assign("/login?reason=expired");
    }
  }
}

/**
 * TanStack Query is the only server-state mechanism (the house doctrine).
 * The 401 teardown covers BOTH caches (scaffold review: queries
 * alone left a revoked session stuck mid-mutation). Mutations never retry
 * (exactly one write attempt per submission — concurrency safety); queries retry once.
 * (Salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238.)
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
