"use client";

/**
 * Adjustment review drawer (the CHECKER phase of the issue- two-phase correction): the full AdjustmentRecordOut plus
 * the checker writes its status allows — approval (`POST …/approval`,
 * EMPTY body: the checker approves the PERSISTED snapshot, v1.1 rule
 * 3) and rejection (`POST …/reject`, version-pinned + REQUIRED rationale). Both are corrections:approve.
 *
 * - FRESH RECORD READ (record class, staleTime 0): every checker write
 *   arms against the freshest available read; the version anchors the
 *   reject body and both idempotency-key materials.
 * - MAKER-CHECKER (blocker (f)): checker affordances mount ONLY inside
 *   MakerCheckerPanel. The maker identity is the CONTRACT's maker_id —
 *   REQUIRED server truth (0025 NOT NULL; the pattern with NO registry fallback needed on this surface), rendered under least
 *   disclosure (bare UUID, short-id convention — never a name/email).
 *   The server enforces maker ≠ checker regardless, plus the 0031
 *   ck_repayment_adjustments_sod DB CHECK (collusion-resistant).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`; approval key material folds the
 *   fresh record VERSION + reload epoch (money-mover rule 4 — the
 *   empty wire body carries no version, so the KEY pins the acted-on
 *   snapshot); reject folds the version it also pins on the wire.
 * - A 409 (snapshot drift under the full lock set — balance, penalty
 *   or status moved since request; NOTHING posted) renders the shared
 *   ConflictBanner's explicit reload-and-re-enter flow with
 *   the remedy stated: reject the drifted request and raise a
 *   fresh one. NOTHING is replayed.
 * - MONEY (blocker (a)): every figure (allocation legs, 0031 snapshot,
 *   approval result) is the SERVER's decimal string rendered verbatim
 *   via fmtKes — nothing summed, netted or re-verified client-side.
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { MakerCheckerPanel } from "@/modules/authz/components/MakerCheckerPanel";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { approveAdjustment, fetchAdjustment, rejectAdjustment } from "../api";
import {
  isTerminalAdjustmentStatus,
  rejectReasonEntrySchema,
  type AdjustmentResult,
} from "../schemas";
import { adjustmentStatusPill } from "./pills";
import styles from "./Corrections.module.css";

/** Bare staff UUID under least disclosure: 8-char prefix, full UUID on
 * title — never a name or email (the short-id convention). */
function ShortId({ id }: Readonly<{ id: string }>) {
  return (
    <span className={styles.mono} title={id}>
      {id.slice(0, 8)}
    </span>
  );
}

export function AdjustmentDetailDrawer({
  adjustmentId,
  onClose,
}: Readonly<{
  adjustmentId: string;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [confirmApprove, setConfirmApprove] = useState(false);
  const [confirmReject, setConfirmReject] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [rejectReasonError, setRejectReasonError] = useState("");
  const [approveResult, setApproveResult] = useState<AdjustmentResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency keys: bumped on
  // every explicit post-conflict reload — a re-entered decision after
  // an acknowledged 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const approveKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const rejectKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const detail = useQuery({
    queryKey: ["corrections", "adjustment", adjustmentId],
    queryFn: () => fetchAdjustment(adjustmentId),
    // Record class: NEVER a stale record into a checker write.
    staleTime: STALE_TIME.record,
  });

  const approve = useMutation({
    // STRUCTURAL freshness guard: the money write refuses to
    // fire without the loaded fresh record — the key material folds
    // the REAL record version, never a sentinel.
    mutationFn: () => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh adjustment record is not loaded — nothing was approved."),
        );
      }
      return approveAdjustment(
        adjustmentId,
        idempotencyKeyFor(
          approveKeySlot.current,
          // Money-mover material (README rule 4): the wire body is
          // DELIBERATELY EMPTY, so the KEY pins the
          // acted-on snapshot — fresh record version + reload epoch.
          // Approval is once-per-record (the workflow machine), so no
          // per-success intent counter exists here.
          JSON.stringify({
            op: "adjustment-approve",
            id: adjustmentId,
            recordVersion: fresh.version,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: (posted) => {
      setConfirmApprove(false);
      // SPENT affordance: the result panel replaces the checker panel.
      setApproveResult(posted);
      setNotice("");
      announce("Adjustment approved — the reversal posted to the ledger.");
      void queryClient.invalidateQueries({ queryKey: ["corrections", "adjustment", adjustmentId] });
      void queryClient.invalidateQueries({ queryKey: ["corrections", "adjustments-register"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["loans", "list"] });
    },
    onError: () => {
      setConfirmApprove(false);
      announce("The adjustment was NOT approved — nothing posted.");
    },
  });

  const reject = useMutation({
    mutationFn: (reason: string) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh adjustment record is not loaded — nothing was rejected."),
        );
      }
      return rejectAdjustment(
        adjustmentId,
        fresh.version,
        reason,
        idempotencyKeyFor(
          rejectKeySlot.current,
          // Versioned-write material (README rule 3): the version is
          // ALSO pinned in the wire body; identical retries reuse the
          // key, a reloaded version or changed rationale rotates it.
          JSON.stringify({
            op: "adjustment-reject",
            id: adjustmentId,
            version: fresh.version,
            reason,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: () => {
      setConfirmReject(false);
      setNotice("");
      announce("Adjustment rejected — the repayment's adjustment slot is free again.");
      void queryClient.invalidateQueries({ queryKey: ["corrections", "adjustment", adjustmentId] });
      void queryClient.invalidateQueries({ queryKey: ["corrections", "adjustments-register"] });
    },
    onError: () => {
      setConfirmReject(false);
      announce("The adjustment was NOT rejected.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Repayment adjustment" onClose={onClose} dismissOnOverlay={false}>
        <div className={styles.muted}>Loading adjustment record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Repayment adjustment" onClose={onClose} dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const mayApprove = can(permissions.data, "corrections", "approve");
  const terminal = isTerminalAdjustmentStatus(record.status);
  const anyPending = approve.isPending || reject.isPending;
  const approveConflict = approve.isError && isConflict(approve.error);
  const rejectConflict = reject.isError && isConflict(reject.error);
  const ownId = getOwnUserId();

  // SERVER TRUTH (the pattern): maker_id is the CONTRACT's maker —
  // required, never null (0025 NOT NULL); no per-tab fallback exists or
  // is needed on this surface. Least disclosure: the bare UUID only.
  const makerLabel =
    record.maker_id === ownId
      ? "You requested this adjustment (server attribution)."
      : `Requesting officer ${record.maker_id.slice(0, 8)} (server attribution).`;

  function reloadRecord() {
    // Explicit reload flow: refetch the record (its snapshot
    // drifted under the lock set, its status moved, or the version
    // advanced); the stale decision is NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["corrections", "adjustment", adjustmentId] });
    setReloadEpoch((epoch) => epoch + 1);
    approve.reset();
    reject.reset();
    setNotice(
      "Record reloaded — nothing was posted. If the loan drifted since the request, reject this adjustment and raise a fresh one to capture current state.",
    );
    announce("Record reloaded after the conflict — nothing was posted.");
  }

  function armReject() {
    if (reject.isPending) return;
    const parsed = rejectReasonEntrySchema.safeParse({ reason: rejectReason.trim() });
    if (!parsed.success) {
      setRejectReasonError(parsed.error.issues[0]?.message ?? "The rationale is required.");
      return;
    }
    setRejectReasonError("");
    setConfirmReject(true);
  }

  return (
    <Modal
      title="Repayment adjustment"
      onClose={onClose}
      closeDisabled={anyPending}
      // W56-3: this drawer bears money-write affordances — a stray
      // overlay click must never discard an in-progress decision.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Status">{adjustmentStatusPill(record.status)}</Kv>
        <Kv label="Adjustment id">
          <span className={styles.mono}>{record.id}</span>
        </Kv>
        <Kv label="Repayment">
          <ShortId id={record.repayment_id} />
        </Kv>
        <Kv label="Loan">
          <ShortId id={record.loan_id} />
        </Kv>
        <Kv label="Original ledger row">
          <ShortId id={record.original_transaction_id} />
        </Kv>
        {record.reversal_transaction_id !== null && (
          <Kv label="Reversal ledger row">
            <ShortId id={record.reversal_transaction_id} />
          </Kv>
        )}
        <Kv label="Maker">
          {/* Contract attribution (the pattern): bare staff UUID,
              short-id convention — never resolved to a name/email. */}
          <ShortId id={record.maker_id} />
        </Kv>
        <Kv label="Checker">
          {record.checker_id !== null ? <ShortId id={record.checker_id} /> : "— (undecided)"}
        </Kv>
        <Kv label="Reason">{record.reason}</Kv>
        <Kv label="Requested">{fmtDateTime(record.created_at)}</Kv>
        <Kv label="Decided">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Record version">{record.version}</Kv>
      </div>

      <div className={styles.subhead}>Allocation being undone (server-derived)</div>
      <div className={styles.detailGrid}>
        <Kv label="Amount">
          <span className={styles.netCell}>{fmtKes(record.amount)}</span>
        </Kv>
        <Kv label="Penalties">
          <span className={styles.amountCell}>{fmtKes(record.penalties)}</span>
        </Kv>
        <Kv label="Interest">
          <span className={styles.amountCell}>{fmtKes(record.interest)}</span>
        </Kv>
        <Kv label="Principal">
          <span className={styles.amountCell}>{fmtKes(record.principal)}</span>
        </Kv>
        <Kv label="Would reopen the loan">{record.would_reopen_loan ? "Yes" : "No"}</Kv>
      </div>

      <div className={styles.subhead}>Approval snapshot (at request, under locks)</div>
      <div className={styles.detailGrid}>
        <Kv label="Loan balance">
          {record.loan_balance_at_request !== null ? (
            <span className={styles.amountCell}>{fmtKes(record.loan_balance_at_request)}</span>
          ) : (
            "—"
          )}
        </Kv>
        <Kv label="Loan penalty due">
          {record.loan_penalty_due_at_request !== null ? (
            <span className={styles.amountCell}>
              {fmtKes(record.loan_penalty_due_at_request)}
            </span>
          ) : (
            "—"
          )}
        </Kv>
        <Kv label="Loan status">{record.loan_status_at_request ?? "—"}</Kv>
      </div>
      <div className={styles.formNote}>
        Every figure above is the SERVER&apos;s — derived from the original
        transaction&apos;s append-only legs and the 0031 approval snapshot;
        nothing is summed or re-verified in this screen. Approval re-verifies
        the snapshot component-by-component under the full lock set and
        returns a conflict on any drift, posting nothing.
      </div>

      {/* One copy of the 409 reload flow (reuse-first). */}
      <ConflictBanner error={approve.error} onReload={reloadRecord} />
      {approve.isError && !approveConflict && <ErrorBanner error={approve.error} />}
      <ConflictBanner error={reject.error} onReload={reloadRecord} />
      {reject.isError && !rejectConflict && <ErrorBanner error={reject.error} />}

      {approveResult !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Reversal posted · ref {approveResult.reversal_txn_ref}
          </div>
          <Kv label="Amount reversed">
            <span className={styles.netCell}>{fmtKes(approveResult.amount)}</span>
          </Kv>
          <Kv label="Loan balance after">
            <span className={styles.amountCell}>{fmtKes(approveResult.balance_after)}</span>
          </Kv>
          <Kv label="Penalty due after">
            <span className={styles.amountCell}>{fmtKes(approveResult.penalty_due_after)}</span>
          </Kv>
          <Kv label="Loan status">{approveResult.loan_status}</Kv>
          <Kv label="Loan reopened">{approveResult.reopened ? "Yes" : "No"}</Kv>
          <Kv label="Reversal ledger row">
            <span className={styles.mono}>{approveResult.reversal_txn_id}</span>
          </Kv>
          <div className={styles.formNote}>
            Every figure above came from the approval response — nothing was
            computed in this screen. The reversal row appears in the
            transactions register.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      )}

      {terminal && approveResult === null && (
        <div className={styles.formNote}>
          This adjustment is {record.status === "posted" ? "posted" : "rejected"} — a terminal
          state. No further checker action exists for it (the server enforces
          the 0031 transition matrix regardless).
        </div>
      )}

      {!terminal && approveResult === null && (
        <>
          <div className={styles.subhead}>Checker decision</div>
          <MakerCheckerPanel
            makerId={record.maker_id}
            makerLabel={makerLabel}
            makerAt={fmtDateTime(record.created_at)}
            checkerActions={
              mayApprove ? (
                <>
                  <div className={styles.formNote}>
                    Approving posts the storno reversal and recomputes the loan
                    from its surviving history. The maker can never check their
                    own request and assurance roles can never be checker (both
                    server-enforced; the maker≠checker rule is also a database
                    CHECK).
                  </div>
                  <FormField
                    id="adj-reject-reason"
                    label="Rejection rationale (required to reject)"
                    error={rejectReasonError === "" ? undefined : rejectReasonError}
                    hint="Four-eyes practice: the checker's reason goes on the audit record; a rejected slot frees for a fresh request."
                  >
                    {(control) => (
                      <textarea
                        {...control}
                        className={styles.textarea}
                        maxLength={500}
                        value={rejectReason}
                        onChange={(event) => setRejectReason(event.target.value)}
                        disabled={anyPending}
                      />
                    )}
                  </FormField>
                  <div className={styles.voteActions}>
                    <Button type="button" variant="danger" onClick={armReject} disabled={anyPending}>
                      Reject adjustment…
                    </Button>
                    <Button
                      type="button"
                      variant="primary"
                      onClick={() => {
                        if (!approve.isPending && detail.data !== undefined) {
                          setConfirmApprove(true);
                        }
                      }}
                      disabled={anyPending}
                    >
                      Approve & post reversal…
                    </Button>
                  </div>
                </>
              ) : (
                <div className={styles.formNote}>
                  Your role has no corrections approval permission — checker
                  controls are not offered.
                </div>
              )
            }
          >
            <div className={styles.formNote}>
              Pending reversal of {fmtKes(record.amount)} (server snapshot,
              re-verified component-by-component at approval).
            </div>
          </MakerCheckerPanel>
        </>
      )}

      {confirmApprove && (
        <ConfirmDangerModal
          title="Approve adjustment"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Approve & post reversal"
          pending={approve.isPending}
          onConfirm={() => {
            if (!approve.isPending) approve.mutate();
          }}
          onClose={() => setConfirmApprove(false)}
        >
          <Banner>
            This posts the reversal of {fmtKes(record.amount)} (the COMPLETE
            original allocation) and restores the loan&apos;s state from its
            surviving history{record.would_reopen_loan ? ", REOPENING the closed loan" : ""}. The
            server re-verifies the persisted snapshot under the full lock set —
            any drift is a conflict with NOTHING posted. The wire body is
            empty by contract: every figure comes from the snapshot.
          </Banner>
        </ConfirmDangerModal>
      )}

      {confirmReject && (
        <ConfirmDangerModal
          title="Reject adjustment"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Reject adjustment"
          pending={reject.isPending}
          onConfirm={() => {
            if (!reject.isPending) reject.mutate(rejectReason.trim());
          }}
          onClose={() => setConfirmReject(false)}
        >
          <Banner>
            This rejects the pending adjustment with your rationale on the
            record and frees the repayment&apos;s one-live-adjustment slot for
            a fresh request. No money moves. The write pins record version{" "}
            {record.version} — any drift is a conflict with nothing changed.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
