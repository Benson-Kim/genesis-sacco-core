"use client";

/**
 * Legacy branch-backfill dialog: `POST
 * /jobs/branch-backfill` (settings:edit — the registry it populates;
 * the POST /jobs/arrears operations convention).
 *
 * - The wire body carries ONLY the server's own documented batch-size
 *   default (see api.ts SERVER_DEFAULT_BATCH_SIZE) — a paging tuning,
 *   never an operator value; no other field exists on the contract
 *   (extra="forbid" server-side).
 * - The run is batched and idempotent SERVER-side: it creates registry
 *   rows from the deprecated legacy users.branch free text and links
 *   the users; a completed re-run scans nothing (anti-join on the
 *   claim key — a lock-free no-op) — PROVEN by the returned
 *   side-effect counts rendered verbatim below, never by prose.
 * - EXACTLY ONE write per intent: pending short-circuit + disabled
 *   controls, one Idempotency-Key per logical intent — material folds
 *   the op discriminator + the canonical body + a per-success run
 *   counter (README MATERIAL rule 4): an intentional RE-RUN after a
 *   success is a NEW intent (the server must scan again, not replay
 *   the stored first-run report); retries of one failed attempt keep
 *   the key stable.
 * - NO money moves and NO money field exists anywhere on this
 *   contract.
 */
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Kv, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { runBranchBackfill, SERVER_DEFAULT_BATCH_SIZE } from "../api";
import type { BackfillRun } from "../schemas";
import styles from "./Branches.module.css";

export function BackfillDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [lastRun, setLastRun] = useState<BackfillRun | null>(null);
  // Per-success run counter (README MATERIAL rule 4): the idempotent
  // RE-RUN after a success is a NEW intent — the server must scan
  // again, not serve the stored first-run report.
  const [runSeq, setRunSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const run = useMutation({
    mutationFn: () =>
      runBranchBackfill(
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "branch-backfill",
            body: { batch_size: SERVER_DEFAULT_BATCH_SIZE },
            run_seq: runSeq,
          }),
        ),
      ),
    onSuccess: (report) => {
      setLastRun(report);
      setRunSeq((seq) => seq + 1);
      announce("Branch backfill run finished — counts reported below.");
      // Registry rows may have been created — refetch truth.
      void queryClient.invalidateQueries({ queryKey: ["branches", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    },
    onError: () => {
      announce("The branch backfill did NOT run.");
    },
  });

  return (
    <Modal
      title="Run legacy branch backfill"
      onClose={onClose}
      closeDisabled={run.isPending}
      variant="dialog"
      dismissOnOverlay={false}
    >
      {run.isError && <ErrorBanner error={run.error} />}

      {lastRun !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Backfill run report</div>
          <Kv label="Legacy rows scanned">{lastRun.scanned}</Kv>
          <Kv label="Branches created">{lastRun.branches_created}</Kv>
          <Kv label="Users linked">{lastRun.users_linked}</Kv>
          <Kv label="Batches">{lastRun.batches}</Kv>
          <div className={styles.formNote}>
            Every count above is the SERVER&apos;s run report. A completed
            re-run scans nothing (the anti-join claim key makes it a
            lock-free no-op) — the zeros are the proof, not this text.
          </div>
        </div>
      )}

      <div className={styles.formNote}>
        Creates branch registry rows from the deprecated legacy free-text
        user records and links the users, for THIS tenant only — batched and
        idempotent server-side. Nothing monetary is touched. The batch size
        sent is the server&apos;s own documented default (
        {SERVER_DEFAULT_BATCH_SIZE}), not an operator choice.
      </div>

      <div className={styles.actions}>
        <Button type="button" onClick={onClose} disabled={run.isPending}>
          {lastRun !== null ? "Close" : "Cancel"}
        </Button>
        <Button
          type="button"
          variant="primary"
          onClick={() => {
            if (!run.isPending) run.mutate();
          }}
          disabled={run.isPending}
        >
          {run.isPending ? "Running…" : lastRun !== null ? "Re-run backfill" : "Run backfill"}
        </Button>
      </div>
    </Modal>
  );
}
