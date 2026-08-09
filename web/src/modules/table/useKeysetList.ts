"use client";

import {
  useInfiniteQuery,
  type QueryKey,
  type UseInfiniteQueryResult,
  type InfiniteData,
} from "@tanstack/react-query";
import { STALE_TIME } from "@/lib/query";
import type { KeysetPage } from "./schemas";

export type KeysetListResult<T> = UseInfiniteQueryResult<
  InfiniteData<KeysetPage<T>, string | null>,
  Error
>;

/**
 * Cursor-driven list state for keyset-paginated endpoints (scalability).
 * `fetchPage(null)` loads the first page; subsequent pages follow the
 * server-issued `next_cursor` — never offsets.
 */
export function useKeysetList<T>(options: {
  queryKey: QueryKey;
  fetchPage: (cursor: string | null) => Promise<KeysetPage<T>>;
}): KeysetListResult<T> {
  return useInfiniteQuery({
    queryKey: options.queryKey,
    queryFn: ({ pageParam }) => options.fetchPage(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.nextCursor,
    // Entity-class staleTime (lib/query.ts): fresh across tab switches
    // without refetch-per-render.
    staleTime: STALE_TIME.list,
  });
}
