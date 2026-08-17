"use client";

import {
  useInfiniteQuery,
  type QueryKey,
  type UseInfiniteQueryResult,
  type InfiniteData,
} from "@tanstack/react-query";
import type { KeysetPage } from "./schemas";

export type KeysetListResult<T> = UseInfiniteQueryResult<
  InfiniteData<KeysetPage<T>, string | null>,
  Error
>;

/**
 * Cursor-driven list state for keyset-paginated endpoints (gate 1.3).
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
  });
}
