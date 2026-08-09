"use client";

/**
 * Shared optimistic-lock conflict banner (one copy, reuse-first). Renders ONLY for HTTP 409: the explicit reload-and-re-enter flow
 * — the stale submission is NEVER replayed and there is NO silent
 * overwrite (the caller's reload handler refetches; re-applying the
 * change is a NEW operator intent with a NEW Idempotency-Key).
 * Renders nothing for every other error so callers can pair it with
 * ErrorBanner without double-reporting.
 */
import { Banner, Button } from "@genesis/design-system";
import { isConflict } from "@/lib/errors";
import styles from "./ConflictBanner.module.css";

export function ConflictBanner({
  error,
  onReload,
}: Readonly<{
  error: unknown;
  /** Refetch the record; must NOT resubmit anything. */
  onReload: () => void;
}>) {
  if (!isConflict(error)) return null;
  return (
    <Banner variant="error">
      This record changed since you loaded it (or conflicts with an existing
      one). Your change was NOT applied.
      <div className={styles.actions}>
        <Button type="button" onClick={onReload}>
          Reload record
        </Button>
      </div>
    </Banner>
  );
}
