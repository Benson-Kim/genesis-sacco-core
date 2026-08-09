"use client";

/**
 * Deposit-interest run dialog (the P11 quarterly
 * accrual's operations catch-up route, `POST /jobs/deposit-interest`,
 * transactions:edit).
 *
 * - The request body carries the batch size ONLY (the contract's sole
 *   caller-tunable field, sent at its documented default): the annual
 *   rate comes exclusively from tenant configuration and the accrued
 *   period is resolved server-side in strict quarter order (v1.1 rule
 *   1) — no rate, no period, no as-of can even be sent. Re-running a
 *   fully accrued tenant is an idempotent server-side no-op.
 * - Money-adjacent write: typed ConfirmDangerModal, ONE
 *   idempotent write (`retry: 0`, pending short-circuit), SPENT
 *   affordance (the result panel replaces the action; a fresh dialog
 *   is a new intent with a new key slot).
 * - A 409 here is a CREATE-style conflict (unconfigured tenant rate or
 *   no fully elapsed quarter) — there is no record version to reload
 *   and re-enter, so it renders the least-disclosure ErrorBanner with
 *   an operator note, not the ConflictBanner.
 *   Nothing is retried or replayed either way.
 * - Every result figure (period, rate, counts, total interest) is the
 *   SERVER's, rendered verbatim (blocker (a)).
 */
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { runDepositInterest } from "../api";
import type { InterestRun } from "../schemas";
import styles from "./Transactions.module.css";

const CONFIRM_PHRASE = "accrue interest";

export function InterestRunDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<InterestRun | null>(null);
  // Freshness component: a 409 here means
  // the run was structurally rejected (rate unconfigured / quarter not
  // elapsed) — once the operator fixes the cause and retries in the
  // SAME dialog, that retry is a NEW intent and must not be served a
  // pinned stale 409 from the backend idempotency store. The epoch
  // bumps on conflict only: pure 5xx retries keep the SAME key.
  const [conflictEpoch, setConflictEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const run = useMutation({
    mutationFn: () =>
      runDepositInterest(
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({ op: "deposit-interest-run", conflict_epoch: conflictEpoch }),
        ),
      ),
    onSuccess: (recorded) => {
      setConfirming(false);
      // SPENT affordance: the result panel replaces the action.
      setResult(recorded);
      announce("Deposit-interest run completed.");
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    },
    onError: (error) => {
      setConfirming(false);
      // Rotate ONLY on the acknowledged conflict (T3); identical 5xx
      // retries stay on the same key.
      if (isConflict(error)) setConflictEpoch((epoch) => epoch + 1);
      announce("The deposit-interest run was NOT started.");
    },
  });

  const conflicted = run.isError && isConflict(run.error);

  return (
    <Modal
      title="Run deposit interest"
      onClose={onClose}
      closeDisabled={run.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never silently discard the armed
      // money-write flow (typed confirmation + conflict-epoch state) —
      // dismissal is the explicit ✕ or Escape. (This module predates the
      // focus-dismissal sweep; convention completed here.)
      dismissOnOverlay={false}
    >
      {run.isError && <ErrorBanner error={run.error} />}
      {conflicted && (
        <div className={styles.formNote}>
          A conflict here means the tenant&apos;s deposit-interest rate is not
          configured (Settings) or no quarter has fully elapsed since the last
          accrual. Nothing was posted and nothing will be retried
          automatically.
        </div>
      )}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Interest accrued · {result.period_start} → {result.period_end}
          </div>
          <Kv label="Annual rate">{result.annual_rate_pct}% (tenant-configured)</Kv>
          <Kv label="Accounts scanned">{result.scanned}</Kv>
          <Kv label="Postings made">{result.posted}</Kv>
          <Kv label="Skipped (already accrued)">{result.skipped_existing}</Kv>
          <Kv label="Rate mismatches">{result.rate_mismatches}</Kv>
          <Kv label="Batches">{result.batches}</Kv>
          <Kv label="Total interest posted">{fmtKes(result.total_interest)}</Kv>
          <div className={styles.formNote}>
            Every figure above is the server&apos;s: the period advanced in
            strict quarter order and the basis is the ledger-reconstructed
            average daily balance — nothing was computed in this screen.
          </div>
        </div>
      )}

      {result === null && (
        <>
          <Banner>
            This accrues quarterly deposit interest for the NEXT unaccrued
            quarter, tenant-wide. The rate comes exclusively from tenant
            configuration and the period is resolved by the server — this
            screen sends neither. A completed quarter re-run posts nothing
            (idempotent server-side).
          </Banner>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={run.isPending}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                if (!run.isPending) setConfirming(true);
              }}
              disabled={run.isPending}
            >
              {run.isPending ? "Running…" : "Run accrual…"}
            </Button>
          </div>
        </>
      )}
      {result !== null && (
        <div className={styles.actions}>
          <Button type="button" onClick={onClose}>
            Close
          </Button>
        </div>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Accrue deposit interest"
          confirmPhrase={CONFIRM_PHRASE}
          confirmLabel="Accrue deposit interest"
          pending={run.isPending}
          onConfirm={() => {
            if (!run.isPending) run.mutate();
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This posts INT- ledger entries to every eligible deposit account
            for the next unaccrued quarter. The posting cannot be undone from
            this screen.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
