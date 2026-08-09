"use client";

/**
 * Loan products — shared reference data (reuse, reuse-first).
 *
 * Lives OUTSIDE any screen component module so cross-screen consumers
 * (applications register, committee, loans, guarantors) never import a
 * screen component's chunk for a hook — importing a screen module for
 * reference data couples the screens' bundles and partially defeats the
 * drawer-level code splitting.
 */
import { useQuery } from "@tanstack/react-query";
import { STALE_TIME } from "@/lib/query";
import { fetchProducts } from "./api";

export const PRODUCTS_QUERY_KEY = ["products", "list"] as const;

/** Loan products — reference data (names for the list, options for intake). */
export function useProducts() {
  // Product names degrade gracefully on failure: the list still works
  // (ids render as inert text) and the server validates every product
  // choice regardless (users-module useRoles precedent).
  return useQuery({
    queryKey: PRODUCTS_QUERY_KEY,
    queryFn: fetchProducts,
    staleTime: STALE_TIME.reference,
  });
}
