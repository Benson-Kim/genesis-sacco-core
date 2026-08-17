"use client";

import type { ReactNode } from "react";
import { ApiError } from "@genesis/api-client";
import { Button } from "@genesis/design-system";
import type { KeysetListResult } from "./useKeysetList";
import styles from "./KeysetTable.module.css";

export interface Column<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  render: (row: T) => ReactNode;
}

export interface KeysetTableProps<T> {
  columns: Column<T>[];
  query: KeysetListResult<T>;
  rowKey: (row: T) => string;
  emptyMessage?: string;
  onRowClick?: (row: T) => void;
}

/**
 * Shared cursor-paginated table (prototype table styling). Used by the
 * members / loan book / ledger screens in P15.
 */
export function KeysetTable<T>({
  columns,
  query,
  rowKey,
  emptyMessage = "Nothing to show yet.",
  onRowClick,
}: Readonly<KeysetTableProps<T>>) {
  if (query.isPending) {
    return <div className={styles.note}>Loading…</div>;
  }

  if (query.isError) {
    const reference =
      query.error instanceof ApiError && query.error.correlationId !== null
        ? ` (ref ${query.error.correlationId})`
        : "";
    return (
      <div className={styles.error} role="alert">
        Could not load this list{reference}. Try again shortly.
      </div>
    );
  }

  const rows = query.data.pages.flatMap((page) => page.items);

  if (rows.length === 0) {
    return <div className={styles.note}>{emptyMessage}</div>;
  }

  return (
    <div>
      <table className={styles.table}>
        <thead>
          <tr className={styles.headRow}>
            {columns.map((column) => (
              <th
                key={column.key}
                className={
                  column.align === "right" ? styles.thRight : styles.th
                }
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className={
                onRowClick ? `${styles.row} ${styles.rowClickable}` : styles.row
              }
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              {...(onRowClick && {
                role: "button",
                tabIndex: 0,
                onKeyDown: (event: React.KeyboardEvent) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onRowClick(row);
                  }
                },
              })}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={column.align === "right" ? styles.tdRight : styles.td}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {query.hasNextPage && (
        <div className={styles.more}>
          <Button
            onClick={() => query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
          >
            {query.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
    </div>
  );
}
