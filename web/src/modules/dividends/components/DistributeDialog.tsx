"use client";

/**
 * Distribution dialog (the tenant-wide mass money-mover): `POST /dividends/declarations/{id}/distribution`
 * (transactions:approve) pays the committee-APPROVED snapshot to every
 * eligible member — batched, idempotent and atomic per member
 * server-side.
 *
 * - The dialog fetches the declaration FRESH (record class, staleTime
 *   0) and offers the action ONLY while the status is `approved` — the
 *   UI never offers what the API forbids (least disclosure).
 * - SEPARATION OF DUTIES (blocker (f)): the run affordance mounts ONLY
 *   inside MakerCheckerPanel — the contract bans the declarer from
 *   executing their own distribution (403, keyed on the PERSISTED
 *   requested_by). The maker identity is the CONTRACT's requested_by
 *   FIRST (server truth, the exits /0036 precedent); the per-tab witness (makerRegistry.ts) is a fallback
 *   for unattributed rows only. Controls are structurally withheld
 *   for the identified maker (no override prop exists); the server
 *   enforces regardless.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit + disabled controls, `retry: 0`, one
 *   Idempotency-Key per logical intent — key material folds the fresh
 *   record VERSION, a post-409 reload epoch and a per-success run
 *   counter (README MATERIAL rule 4): an idempotent RE-RUN after a
 *   partial run (pending_members > 0 — members skipped under
 *   concurrent locks) is a NEW intent that must never be served the
 *   first run's stored response; retries of one failed attempt keep
 *   the key stable. The wire body carries only the server's own
 *   documented batch-size default: every figure
 *   derives from the persisted approval snapshot.
 * - A 409 (snapshot drift on first-run verification, a concurrent
 *   void, or a non-approved status) renders the shared
 *   ConflictBanner's explicit reload-and-re-enter flow with an
 *   INFORMATIONAL notice + announce(). The documented remedy
 *   for drift is voiding and re-declaring — stated to the operator;
 *   NOTHING is replayed.
 * - The run REPORT renders VERBATIM (blocker (a)): claim counts, the
 *   issue- unclaimed dispositions (mid-run exits parked as a
 *   payable, never credited to a closed account, never dropped) and
 *   the server's totals. A partial run keeps the affordance armed for
 *   the idempotent re-run; a completed run (status `distributed`)
 *   spends it.
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { MakerCheckerPanel } from "@/modules/authz/components/MakerCheckerPanel";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { distributeDeclaration, fetchDeclaration, SERVER_DEFAULT_BATCH_SIZE } from "../api";
import { DIVIDEND_MAKER_UNKNOWN, dividendMakerOf } from "../makerRegistry";
import type { DistributionRun } from "../schemas";
import styles from "./Dividends.module.css";

export function DistributeDialog({
  declarationId,
  onClose,
}: Readonly<{
  declarationId: string;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [lastRun, setLastRun] = useState<DistributionRun | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success run counter (README MATERIAL rule 4 / W59-3): the
  // idempotent RE-RUN after a partial run is a NEW intent — the server
  // must scan again, not replay the stored first-run report.
  const [runSeq, setRunSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Record class (staleTime 0): NEVER a stale snapshot into the mass
  // money write — the version below is the freshest read.
  const detail = useQuery({
    queryKey: ["dividends", "detail", declarationId],
    queryFn: () => fetchDeclaration(declarationId),
    staleTime: STALE_TIME.record,
  });

  const distribute = useMutation({
    // STRUCTURAL freshness guard (the F-M1 class): the mass money
    // write refuses to fire without the loaded fresh record — its key
    // material folds the REAL record version, never a null sentinel.
    // (The wire body carries only the server's batch-size default;
    // the version rides the KEY to pin the intent to the snapshot
    // state the operator saw.)
    mutationFn: () => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh declaration is not loaded — the distribution was NOT started."),
        );
      }
      return distributeDeclaration(
        declarationId,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "dividend-distribute",
            id: declarationId,
            body: { batch_size: SERVER_DEFAULT_BATCH_SIZE },
            recordVersion: fresh.version,
            reload_epoch: reloadEpoch,
            run_seq: runSeq,
          }),
        ),
      );
    },
    onSuccess: (run) => {
      setConfirming(false);
      setLastRun(run);
      // The NEXT run (picking up SKIP LOCKED-skipped members) is a NEW
      // intent even though the request body is byte-identical.
      setRunSeq((seq) => seq + 1);
      setNotice("");
      announce(
        run.status === "distributed"
          ? "Dividend distribution completed — the declaration is fully reconciled."
          : "Distribution run finished with members still pending — re-run to pick them up.",
      );
      // Claims, member balances and the ledger moved — refetch truth.
      void queryClient.invalidateQueries({ queryKey: ["dividends", "detail", declarationId] });
      void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirming(false);
      announce("The dividend distribution did NOT run.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal
        title="Run dividend distribution"
        onClose={onClose}
        variant="dialog"
        dismissOnOverlay={false}
      >
        <div className={styles.muted}>Loading declaration…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal
        title="Run dividend distribution"
        onClose={onClose}
        variant="dialog"
        dismissOnOverlay={false}
      >
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const conflict = distribute.isError && isConflict(distribute.error);
  // A PARTIAL run keeps the affordance armed (idempotent re-run picks
  // up the skipped members); only a non-approved status withdraws it.
  const runnable = record.status === "approved";
  const completed = lastRun !== null && lastRun.status === "distributed";

  // SERVER TRUTH FIRST (the exits /0036
  // precedent): requested_by is the contract's declarer attribution;
  // the per-tab witness (makerRegistry.ts) is a fallback for
  // unattributed rows ONLY — nothing is invented. The server 403s the
  // declarer's self-distribution on the persisted column regardless
  // (least disclosure).
  const witnessedMakerId = dividendMakerOf(record.id);
  const makerId = record.requested_by ?? witnessedMakerId;
  const ownId = getOwnUserId();
  const makerLabel =
    record.requested_by !== null
      ? record.requested_by === ownId
        ? "You declared this dividend (server attribution)."
        : // Least disclosure: the bare staff UUID via the short-id
          // convention — never a name or email.
          `Declaring officer ${record.requested_by.slice(0, 8)} (server attribution).`
      : witnessedMakerId === DIVIDEND_MAKER_UNKNOWN
        ? "Declaring officer — unattributed on the server record (attribution is never invented); no declaration witnessed by this tab. The server still refuses the declarer's own distribution."
        : witnessedMakerId === ownId
          ? "You declared this dividend (witnessed by this tab)."
          : "Declaring officer (witnessed by this tab).";

  function reloadRecord() {
    // Explicit reload flow: refetch the record (the snapshot
    // drifted on verification, a void raced the run, or the status
    // moved) and the register; the stale run is NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["dividends", "detail", declarationId] });
    void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    distribute.reset();
    setNotice(
      "Record reloaded — nothing was posted by the conflicted attempt. If the snapshot drifted since approval, void this declaration and declare afresh to capture current figures.",
    );
    announce("Record reloaded after the conflict — nothing was posted by the conflicted attempt.");
  }

  return (
    <Modal
      title="Run dividend distribution"
      onClose={onClose}
      closeDisabled={distribute.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never discard the half-armed
      // tenant-wide money write.
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling (W59-4, the F5 class). */}
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Declaration">
          <span className={styles.mono} title={record.id}>
            {record.id.slice(0, 8)}
          </span>
        </Kv>
        <Kv label="Status">{record.status}</Kv>
        <Kv label="Financial year">
          {record.fy_start} → {record.fy_end}
        </Kv>
        <Kv label="Approved">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Eligible members (incl. dormant)">{record.eligible_members}</Kv>
        <Kv label="Dividend">{fmtKes(record.total_dividend)}</Kv>
        <Kv label="Rebate">{fmtKes(record.total_rebate)}</Kv>
        <Kv label="Total payout">
          <span className={styles.payoutCell}>{fmtKes(record.total_payout)}</span>
        </Kv>
        <Kv label="Pinned record version">{record.version}</Kv>
      </div>

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={distribute.error} onReload={reloadRecord} />
      {distribute.isError && !conflict && <ErrorBanner error={distribute.error} />}

      {lastRun !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            {lastRun.status === "distributed"
              ? "Distribution completed · declaration reconciled"
              : "Distribution run report · members still pending"}
          </div>
          <Kv label="Members scanned">{lastRun.scanned}</Kv>
          <Kv label="Paid this run">{lastRun.claimed}</Kv>
          <Kv label="Skipped (zero entitlement)">{lastRun.skipped_zero}</Kv>
          <Kv label="Skipped (already paid)">{lastRun.skipped_claimed}</Kv>
          <Kv label="Unclaimed dispositions (mid-run exits)">{lastRun.unclaimed}</Kv>
          <Kv label="Dividend paid this run">{fmtKes(lastRun.dividend_total)}</Kv>
          <Kv label="Rebate paid this run">{fmtKes(lastRun.rebate_total)}</Kv>
          <Kv label="Payout this run">
            <span className={styles.payoutCell}>{fmtKes(lastRun.payout_total)}</span>
          </Kv>
          <Kv label="Members still pending">{lastRun.pending_members}</Kv>
          <Kv label="Batches">{lastRun.batches}</Kv>
          <Kv label="Declaration status">{lastRun.status}</Kv>
          <div className={styles.formNote}>
            Every figure above came from the run report — nothing was computed
            in this screen. &quot;Unclaimed&quot; are members who exited
            mid-run: their entitlement is parked as an unclaimed-dividends
            payable, never credited to a closed account and
            never silently dropped — resolve through the corrections paths.
            {lastRun.pending_members > 0 &&
              " Pending members were skipped under concurrent locks — re-run the distribution to pick them up; already-paid members are never paid twice."}
          </div>
          {completed && (
            <div className={styles.actions}>
              <Button type="button" onClick={onClose}>
                Close
              </Button>
            </div>
          )}
        </div>
      )}

      {runnable && (
        <MakerCheckerPanel
          makerId={makerId}
          makerLabel={makerLabel}
          makerAt={fmtDateTime(record.created_at)}
          checkerActions={
            <>
              <div className={styles.formNote}>
                The run is idempotent and batched: already-paid members are
                skipped, mid-run exits are parked as unclaimed payables, and
                concurrency-skipped members are picked up by a re-run. The
                first run re-verifies the approved snapshot against live
                balances and posts NOTHING on drift.
              </div>
              <div className={styles.actions}>
                <Button type="button" onClick={onClose} disabled={distribute.isPending}>
                  {lastRun !== null ? "Close" : "Cancel"}
                </Button>
                <Button
                  type="button"
                  variant="primary"
                  onClick={() => {
                    if (distribute.isPending) return;
                    // F-M1 belt: the confirmation cannot even ARM
                    // without the loaded fresh record.
                    if (detail.data === undefined) return;
                    setConfirming(true);
                  }}
                  disabled={distribute.isPending}
                >
                  {distribute.isPending
                    ? "Running…"
                    : lastRun !== null
                      ? "Re-run distribution…"
                      : "Run distribution…"}
                </Button>
              </div>
            </>
          }
        >
          <div className={styles.formNote}>
            Committee-approved payout awaiting distribution:{" "}
            {fmtKes(record.total_payout)} across {record.eligible_members} members
            (server snapshot, re-verified before any money moves).
          </div>
        </MakerCheckerPanel>
      )}
      {!runnable && lastRun === null && (
        <div className={styles.formNote}>
          This declaration is not in the approved status — distribution is not
          offered (the server enforces the transition matrix regardless).
        </div>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Run dividend distribution"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Run distribution"
          pending={distribute.isPending}
          onConfirm={() => {
            if (!distribute.isPending) distribute.mutate();
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This moves REAL money tenant-wide: the approved payout of{" "}
            {fmtKes(record.total_payout)} is credited to every eligible
            member&apos;s deposits (postings dated to the financial-year end).
            The run cannot be undone from this screen; a partial run is
            completed by re-running, never by paying anyone twice.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
