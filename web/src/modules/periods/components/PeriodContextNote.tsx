"use client";

/**
 * Period-context note (the remedy: "operators cannot see period-close state next to the figures they read; statement readers have no period context"). Mounted beside
 * the figure-bearing surfaces (transactions register, reports).
 *
 * - SELF-GATING: `GET /accounting-periods` is transactions:view — the
 *   note renders NOTHING for a role without that grant (it never
 *   probes an endpoint the operator cannot read; the server enforces
 *   regardless). This keeps the mount unconditional at the routes.
 * - HONESTY: the latest CLOSED month is the server's newest-first
 *   first row; months after it are open BY DESIGN (absence of a row = open) — stated, not computed. On a read failure the
 *   note says so quietly instead of implying "everything open".
 * - NO money and NO client-side date arithmetic: dates render
 *   verbatim; nothing is derived here.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { STALE_TIME } from "@/lib/query";
import { fmtDateTime } from "@/lib/format";
import { fetchPeriodsPage } from "../api";
import { periodStatusPill } from "./pills";
import styles from "./Periods.module.css";

export function PeriodContextNote() {
  const permissions = usePermissions();
  const mayReadPeriods = can(permissions.data, "transactions", "view");

  // Composite-class staleness (lib/query.ts): a context snapshot, not
  // an optimistic-locked record. First page only — the latest closed
  // month IS the server's newest-first first row.
  const context = useQuery({
    queryKey: ["periods", "context"],
    queryFn: () => fetchPeriodsPage(null),
    enabled: mayReadPeriods,
    staleTime: STALE_TIME.composite,
  });

  if (!mayReadPeriods) return null;
  if (context.isPending) return null;

  if (context.isError) {
    return (
      <div className={styles.contextNote}>
        Accounting-period context is unavailable right now — close-state
        could not be read.
      </div>
    );
  }

  const latestClosed = context.data.items.find((period) => period.status === "closed") ?? null;

  return (
    <div className={styles.contextNote}>
      {latestClosed === null ? (
        <span>
          <span className={styles.contextStrong}>Accounting periods:</span> no
          month has been closed yet — every month is open for posting.
        </span>
      ) : (
        <span>
          <span className={styles.contextStrong}>Accounting periods:</span>{" "}
          books closed through{" "}
          <span className={styles.contextStrong}>{latestClosed.period_end}</span>{" "}
          {periodStatusPill(latestClosed.status)} (closed{" "}
          {latestClosed.closed_at === null ? "—" : fmtDateTime(latestClosed.closed_at)}
          ) — later months are open for posting.
        </span>
      )}
      <Link href="/modules/transactions/periods">View period register</Link>
    </div>
  );
}
