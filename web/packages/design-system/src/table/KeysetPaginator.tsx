"use client";

/**
 * Keyset pagination UI — reusable footer + state hook.
 *
 * DESIGN CONTRACT:
 * ───────────────
 * Keyset pagination has no server-side total count (by design — scalability).
 * We work around this honestly:
 *
 *   - All fetched rows are accumulated in memory by `useInfiniteQuery`.
 *   - The paginator slices that flat array into pages of `pageSize` for display.
 *   - When the user navigates forward to a page whose rows haven't been fetched
 *     yet, `fetchNextPage()` is triggered automatically.
 *   - Page numbers are real (1-based) within the accumulated data.
 *   - "Total pages" is shown as "n+" when the server has more data, or "n"
 *     when the last cursor returned null.
 *
 * EXPORTS:
 *   useKeysetPagination — hook that owns pageSize + pageIndex state
 *   KeysetPaginator     — footer component (prev/next, page indicator, size picker)
 */
import { useState, useEffect } from "react";
import type { KeysetListResult } from "./useKeysetList";
import styles from "./KeysetPaginator.module.css";

// ─── Page size options ────────────────────────────────────────────────────────

export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;
export type PageSizeOption = (typeof PAGE_SIZE_OPTIONS)[number];
export const DEFAULT_PAGE_SIZE: PageSizeOption = 10;

// ─── Hook ────────────────────────────────────────────────────────────────────

export interface KeysetPaginationState {
  pageSize: PageSizeOption;
  pageIndex: number; // 0-based
  setPageSize: (size: PageSizeOption) => void;
  setPageIndex: (index: number) => void;
}

/**
 * Owns page-size and page-index state for a KeysetTable + KeysetPaginator pair.
 * Changing `pageSize` resets back to page 0.
 */
export function useKeysetPagination(
  initialPageSize: PageSizeOption = DEFAULT_PAGE_SIZE,
): KeysetPaginationState {
  const [pageSize, setPageSizeRaw] = useState<PageSizeOption>(initialPageSize);
  const [pageIndex, setPageIndex] = useState(0);

  function setPageSize(size: PageSizeOption) {
    setPageSizeRaw(size);
    setPageIndex(0); // reset to first page on size change
  }

  return { pageSize, pageIndex, setPageSize, setPageIndex };
}

// ─── Component ───────────────────────────────────────────────────────────────

export interface KeysetPaginatorProps<T> {
  query: KeysetListResult<T>;
  /** Total number of rows currently loaded (all fetched pages flattened). */
  loadedRowCount: number;
  /** Current 0-based page index. */
  pageIndex: number;
  /** Rows per page. */
  pageSize: PageSizeOption;
  /** Called when the user navigates. Receives the new 0-based page index. */
  onPageChange: (index: number) => void;
  /** Called when the user picks a different page size. */
  onPageSizeChange: (size: PageSizeOption) => void;
  /** Label for the entity type (default: "rows"). */
  rowLabel?: string;
}

export function KeysetPaginator<T>({
  query,
  loadedRowCount,
  pageIndex,
  pageSize,
  onPageChange,
  onPageSizeChange,
  rowLabel = "rows",
}: Readonly<KeysetPaginatorProps<T>>) {
  const hasMore = query.hasNextPage ?? false;
  const isLoadingMore = query.isFetchingNextPage;

  // Total pages we can show from accumulated data.
  const loadedPageCount = Math.max(1, Math.ceil(loadedRowCount / pageSize));
  // Last page index reachable without a new fetch.
  const lastLoadedPage = loadedPageCount - 1;
  // True when there may be more pages beyond what's loaded.
  const morePages = hasMore;

  // 1-based range for display: "1–10 of 10+"
  const rangeStart = loadedRowCount === 0 ? 0 : pageIndex * pageSize + 1;
  const rangeEnd = Math.min((pageIndex + 1) * pageSize, loadedRowCount);

  // Auto-fetch next page when the user navigates to a page that needs it.
  useEffect(() => {
    const neededRows = (pageIndex + 1) * pageSize;
    if (neededRows > loadedRowCount && hasMore && !isLoadingMore) {
      query.fetchNextPage();
    }
  }, [pageIndex, pageSize, loadedRowCount, hasMore, isLoadingMore, query]);

  if (!query.isSuccess) return null;
  // Nothing to paginate: single page, no more data, fits within page size.
  if (loadedRowCount <= pageSize && !hasMore) return null;

  const canPrev = pageIndex > 0;
  const canNext = pageIndex < lastLoadedPage || morePages;

  function goTo(index: number) {
    if (index < 0) return;
    onPageChange(index);
  }

  return (
    <div className={styles.paginator}>
      {/* Left: "Show n per page" size picker */}
      <label className={styles.sizeLabel}>
        Show
        <select
          className={styles.sizePicker}
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value) as PageSizeOption)}
        >
          {PAGE_SIZE_OPTIONS.map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
        per page
      </label>

      {/* Right: ‹ Previous · Page n of n+ · Next › */}
      <div className={styles.nav}>
        <button
          type="button"
          className={styles.navBtn}
          onClick={() => goTo(pageIndex - 1)}
          disabled={!canPrev || isLoadingMore}
          aria-label="Previous page"
        >
          <span className={styles.arrow} aria-hidden="true">‹</span>
          Previous
        </button>

        <span
          className={styles.pageIndicator}
          aria-label={`Showing ${rangeStart}\u2013${rangeEnd} of the loaded ${rowLabel}`}
        >
          {isLoadingMore ? (
            <span className={styles.muted}>Loading…</span>
          ) : (
            <>
              Page{" "}
              <strong>{pageIndex + 1}</strong>
              {" of "}
              <strong>{loadedPageCount}{morePages ? "+" : ""}</strong>
            </>
          )}
        </span>

        <button
          type="button"
          className={styles.navBtn}
          onClick={() => goTo(pageIndex + 1)}
          disabled={!canNext || isLoadingMore}
          aria-label="Next page"
        >
          Next
          <span className={styles.arrow} aria-hidden="true">›</span>
        </button>
      </div>
    </div>
  );
}
