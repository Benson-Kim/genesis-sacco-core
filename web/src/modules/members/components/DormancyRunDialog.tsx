"use client";

/**
 * Dormancy run dialog (the nightly job's operations catch-up route, `POST /members/jobs/dormancy`, members:edit; the backfill / interest-run precedent).
 *
 * - The request body carries the batch size ONLY (the contract's sole
 *   caller-tunable field, sent at its documented default): the
 *   dormancy period resolves exclusively from tenant settings
 *   server-side (extra="forbid") and as_of is
 *   deliberately not sent (the server resolves today). No money moves
 *   and NO money field exists anywhere on this contract.
 * - Status-rewriting write: typed ConfirmDangerModal, ONE
 *   idempotent write (pending short-circuit), SPENT affordance (the
 *   result panel replaces the action; a re-run is a NEW intent with a
 *   rotated key — README MATERIAL rule 4 per-success counter).
 * - A 409 is a CREATE-style conflict (dormancy period unconfigured or
 *   corrupt — the run fails CLOSED with zero transitions): no record
 *   version exists to reload, so it renders the least-disclosure
 *   ErrorBanner with an operator note; the acknowledged
 *   conflict rotates the key (T3) so a post-fix retry is a NEW
 *   intent. Nothing is replayed either way.
 * - Every result figure (dates, period, counts) is the SERVER's,
 *   rendered VERBATIM — activity is never derived client-side.
 */
import { useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { runDormancyJob, SERVER_DORMANCY_BATCH_SIZE } from "../api";
import type { DormancyRun } from "../schemas";
import styles from "./Members.module.css";

function Row({ label, children }: Readonly<{ label: string; children: ReactNode }>) {
  return (
    <div className={styles.summaryRow}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const CONFIRM_PHRASE = "mark dormant";

export function DormancyRunDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<DormancyRun | null>(null);
  // Per-success run counter + conflict epoch (README MATERIAL rules
  // 3/4, the interest-run precedent): an intentional RE-RUN after a
  // success is a NEW intent (the server must scan again), and a retry
  // after an acknowledged 409 (period fixed in Settings) is too; pure
  // 5xx retries keep the SAME key.
  const [runSeq, setRunSeq] = useState(0);
  const [conflictEpoch, setConflictEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const run = useMutation({
    mutationFn: () =>
      runDormancyJob(
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "dormancy-run",
            body: { batch_size: SERVER_DORMANCY_BATCH_SIZE },
            run_seq: runSeq,
            conflict_epoch: conflictEpoch,
          }),
        ),
      ),
    onSuccess: (report) => {
      setConfirming(false);
      setResult(report);
      setRunSeq((seq) => seq + 1);
      announce("Dormancy run finished — counts reported below.");
      // Statuses may have moved — refetch the register truth.
      void queryClient.invalidateQueries({ queryKey: ["members", "list"] });
    },
    onError: (error) => {
      setConfirming(false);
      if (isConflict(error)) setConflictEpoch((epoch) => epoch + 1);
      announce("The dormancy run did NOT start.");
    },
  });

  const conflicted = run.isError && isConflict(run.error);

  return (
    <Modal
      title="Run dormancy scan"
      onClose={onClose}
      closeDisabled={run.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never silently discard the
      // armed run flow — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      {run.isError && <ErrorBanner error={run.error} />}
      {conflicted && (
        <p className={styles.panelNote}>
          A conflict here means the tenant&apos;s dormancy period is not
          configured (Settings) or is corrupt — the run REFUSED with zero
          transitions (fail closed). Nothing changed and nothing will be
          retried automatically.
        </p>
      )}

      {result !== null && (
        <div className={styles.summaryPanel} role="status">
          <div className={styles.panelSubTitle}>Dormancy run report</div>
          <dl className={styles.summaryList}>
            <Row label="As of">{result.as_of}</Row>
            <Row label="Activity cutoff">{result.cutoff}</Row>
            <Row label="Dormancy period (months)">{result.period_months}</Row>
            <Row label="Members scanned">{result.scanned}</Row>
            <Row label="Transitioned to dormant">{result.transitioned}</Row>
            <Row label="Batches">{result.batches}</Row>
          </dl>
          <p className={styles.panelNote}>
            Every figure above is the SERVER&apos;s run report: inactivity is
            derived from the ledger by the job under each member&apos;s row
            lock — never computed in this screen. A re-run inside the same
            window legitimately transitions nobody.
          </p>
        </div>
      )}

      {result === null && (
        <>
          <Banner>
            This scans the tenant for members with no member-initiated ledger
            activity inside the tenant-configured window and transitions them
            Active → Dormant (the nightly job&apos;s manual catch-up). The
            period comes exclusively from tenant settings and the as-of date
            is resolved by the server — this screen sends neither.
            Reactivation happens automatically on deposit, never here.
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
              {run.isPending ? "Running…" : "Run dormancy scan…"}
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
          title="Confirm dormancy scan"
          confirmPhrase={CONFIRM_PHRASE}
          confirmLabel="Run dormancy scan"
          pending={run.isPending}
          onConfirm={() => {
            if (!run.isPending) run.mutate();
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            Members inside the configured inactivity window will be marked
            DORMANT. Dormant members reactivate automatically on their next
            deposit (with a member notification) — no manual undo exists on
            this screen.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
