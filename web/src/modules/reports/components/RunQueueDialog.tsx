"use client";

/**
 * Export queue-run dialog (module 8 — `POST /jobs/exports`, reports:view: the operations mirror of the worker loop, the interest-run precedent adapted to a non-money job).
 *
 * - The contract defines NO request body: nothing tunable can even be
 *   sent — each queued job renders server-side in its own snapshot
 *   transaction with the scope/columns frozen at request time, so
 *   running the queue grants the runner nothing (backend reports.py).
 * - ONE idempotent write (`retry: 0` via the Providers mutation
 *   default, pending short-circuit), SPENT affordance (the result
 *   panel with the SERVER's completed/failed counts replaces the
 *   action; a fresh dialog is a new intent with a new key slot).
 *   No typed ConfirmDangerModal: draining the queue moves no money and
 *   destroys nothing.
 * - The POST has an EMPTY body, so the key material carries an intent
 *   counter bumped on every success (review, mirroring RequestExportDrawer's T2 defence): a SECOND explicit drain — "Run
 *   again" in the same mount, or a fresh dialog — always carries a NEW
 *   key BY CONSTRUCTION. Without it, a backend idempotency store would
 *   dedup the repeat and replay the FIRST drain's completed/failed
 *   counts while the operator believes a fresh drain ran (the W59-3/T2
 *   class). Pure retries of a failed attempt still REUSE the key.
 * - Errors render the least-disclosure ErrorBanner; a 409 (concurrent
 *   identical run holding the idempotency claim) is a CREATE-style
 *   conflict with no record to reload (F-precedent) — the
 *   epoch rotates the key for the NEXT explicit attempt only; nothing
 *   is retried or replayed automatically.
 */
import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Kv, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { runExportQueue } from "../api";
import type { ExportCycle } from "../schemas";
import styles from "./Reports.module.css";

export function RunQueueDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const [result, setResult] = useState<ExportCycle | null>(null);
  // Freshness component (class): rotate the key ONLY after an
  // acknowledged conflict; pure 5xx retries keep the SAME key.
  const [conflictEpoch, setConflictEpoch] = useState(0);
  // Per-drain intent counter (review — the W59-3/T2 class): the
  // wire body is EMPTY, so without this a repeated explicit drain would
  // present the SAME key and a backend idempotency store would replay
  // the FIRST drain's counts instead of draining again. Bumped on every
  // success: rotation by construction, not by remount accident.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const run = useMutation({
    mutationFn: () =>
      runExportQueue(
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "export-queue-run",
            conflict_epoch: conflictEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (cycle) => {
      // SPENT affordance: the result panel replaces the action.
      setResult(cycle);
      setIntentSeq((seq) => seq + 1);
      announce("Export queue run completed.");
    },
    onError: (error) => {
      if (isConflict(error)) setConflictEpoch((epoch) => epoch + 1);
      announce("The export queue run did NOT complete.");
    },
  });

  const conflicted = run.isError && isConflict(run.error);

  return (
    <Modal
      title="Run export queue"
      onClose={onClose}
      closeDisabled={run.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never silently discard the
      // armed run flow — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      {run.isError && <ErrorBanner error={run.error} />}
      {conflicted && (
        <div className={styles.formNote}>
          A conflict here means another identical queue run is still in
          flight. Nothing ran twice and nothing will be retried
          automatically.
        </div>
      )}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Export queue drained</div>
          <Kv label="Jobs completed">{result.completed}</Kv>
          <Kv label="Jobs failed">{result.failed}</Kv>
          <div className={styles.formNote}>
            Both counts are the server&apos;s. Refresh an export&apos;s row on
            the Reports screen to pick up its download links.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                // A NEW drain intent (mirrors "Request another"):
                // the bumped intent counter already rotated the key, so
                // this second explicit drain can never be deduped into
                // the first drain's stored response.
                setResult(null);
                run.reset();
              }}
            >
              Run again
            </Button>
          </div>
        </div>
      )}

      {result === null && (
        <>
          <Banner>
            This renders every queued export for your tenant — each job in
            its own consistent server snapshot, with the scope and column
            allow-list frozen when the export was requested. Running the
            queue changes no data and grants no extra access.
          </Banner>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={run.isPending}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                if (!run.isPending) run.mutate();
              }}
              disabled={run.isPending}
            >
              {run.isPending ? "Running…" : "Run queue"}
            </Button>
          </div>
        </>
      )}
    </Modal>
  );
}
