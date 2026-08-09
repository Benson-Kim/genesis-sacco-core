"use client";

/**
 * Share-transfer review drawer (the CHECKER phase of the ledger-(l) two-phase transfer): the full
 * ShareTransferRecordOut plus the checker writes its status allows —
 * approval (`POST …/approval`, EMPTY body: the checker approves the
 * PERSISTED snapshot) and rejection (`POST …/rejection`, version-pinned + REQUIRED rationale). Both members:approve.
 *
 * - FRESH RECORD READ (record class, staleTime 0): every checker
 *   write arms against the freshest available read; the version
 *   anchors the reject body and both idempotency-key materials.
 * - MAKER-CHECKER (blocker (f)): checker affordances mount ONLY
 *   inside MakerCheckerPanel. The maker identity is the CONTRACT's
 *   created_by — SERVER TRUTH (the pattern; no per-tab registry fallback is needed: every workflow row carries its maker). A NULL
 *   created_by is honest pre-0040 history (also terminal — pre-0040
 *   rows are 'posted'): no checker action exists for it. The server
 *   enforces maker ≠ checker regardless, plus the 0040
 *   ck_share_transfers_sod DB CHECK (collusion-resistant) and the
 *   assurance-role exclusion (B2).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`; approval key material folds
 *   the fresh record VERSION + reload epoch (money-mover rule 4 — the
 *   empty wire body carries no version, so the KEY pins the acted-on
 *   snapshot); reject folds the version it also pins on the wire.
 * - A 409 (snapshot drift under the full lock set — the transferor's
 *   balance moved, a member left ACTIVE, or the status advanced;
 *   NOTHING posted) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow with the house remedy stated:
 *   reject the drifted request and raise a fresh one. NOTHING is
 *   replayed.
 * - MONEY (blocker (a)): every figure is the SERVER's decimal string
 *   rendered verbatim via fmtKes — nothing summed, netted or
 *   re-verified client-side; the register record deliberately carries
 *   NO balance snapshot (least disclosure — the audit rows hold the
 *   exact figures).
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
import { approveShareTransfer, fetchShareTransfer, rejectShareTransfer } from "../api";
import {
  isTerminalTransferStatus,
  rejectReasonEntrySchema,
  type ShareTransferResult,
} from "../schemas";
import { transferStatusPill } from "./pills";
import styles from "./Shares.module.css";

/** Bare UUID under least disclosure: 8-char prefix, full UUID on
 * title — never a name or email (the short-id convention). */
function ShortId({ id }: Readonly<{ id: string }>) {
  return (
    <span className={styles.mono} title={id}>
      {id.slice(0, 8)}
    </span>
  );
}

export function TransferDetailDrawer({
  transferId,
  onClose,
}: Readonly<{
  transferId: string;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [confirmApprove, setConfirmApprove] = useState(false);
  const [confirmReject, setConfirmReject] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [rejectReasonError, setRejectReasonError] = useState("");
  const [approveResult, setApproveResult] = useState<ShareTransferResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency keys: bumped on
  // every explicit post-conflict reload — a re-entered decision after
  // an acknowledged 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const approveKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const rejectKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const detail = useQuery({
    queryKey: ["shares", "transfer", transferId],
    queryFn: () => fetchShareTransfer(transferId),
    // Record class: NEVER a stale record into a checker write.
    staleTime: STALE_TIME.record,
  });

  const approve = useMutation({
    // STRUCTURAL freshness guard: the money write refuses to fire
    // without the loaded fresh record — the key material folds the
    // REAL record version, never a sentinel.
    mutationFn: () => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh transfer record is not loaded — nothing was approved."),
        );
      }
      return approveShareTransfer(
        transferId,
        idempotencyKeyFor(
          approveKeySlot.current,
          // Money-mover material (README rule 4): the wire body is
          // DELIBERATELY EMPTY, so the KEY pins the
          // acted-on snapshot — fresh record version + reload epoch.
          // Approval is once-per-record (the workflow machine), so no
          // per-success intent counter exists here.
          JSON.stringify({
            op: "share-transfer-approve",
            id: transferId,
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
      announce("Share transfer approved — both ledger legs posted; both members notified.");
      void queryClient.invalidateQueries({ queryKey: ["shares", "transfer", transferId] });
      void queryClient.invalidateQueries({ queryKey: ["shares", "transfers-register"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    },
    onError: () => {
      setConfirmApprove(false);
      announce("The share transfer was NOT approved — nothing posted.");
    },
  });

  const reject = useMutation({
    mutationFn: (reason: string) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh transfer record is not loaded — nothing was rejected."),
        );
      }
      return rejectShareTransfer(
        transferId,
        fresh.version,
        reason,
        idempotencyKeyFor(
          rejectKeySlot.current,
          // Versioned-write material (README rule 3): the version is
          // ALSO pinned in the wire body; identical retries reuse the
          // key, a reloaded version or changed rationale rotates it.
          JSON.stringify({
            op: "share-transfer-reject",
            id: transferId,
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
      announce("Share transfer rejected — no money moved; the rationale is on the record.");
      void queryClient.invalidateQueries({ queryKey: ["shares", "transfer", transferId] });
      void queryClient.invalidateQueries({ queryKey: ["shares", "transfers-register"] });
    },
    onError: () => {
      setConfirmReject(false);
      announce("The share transfer was NOT rejected.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Share transfer" onClose={onClose} dismissOnOverlay={false}>
        <div className={styles.muted}>Loading transfer record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Share transfer" onClose={onClose} dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const mayApprove = can(permissions.data, "members", "approve");
  const terminal = isTerminalTransferStatus(record.status);
  const anyPending = approve.isPending || reject.isPending;
  const approveConflict = approve.isError && isConflict(approve.error);
  const rejectConflict = reject.isError && isConflict(reject.error);
  const ownId = getOwnUserId();

  // SERVER TRUTH (the pattern): created_by is the CONTRACT's
  // maker. NULL only on pre-0040 history rows (which are terminal —
  // no checker surface exists for them). Least disclosure: the bare
  // UUID only.
  const makerLabel =
    record.created_by === null
      ? "Pre-workflow history row (no maker attribution recorded)."
      : record.created_by === ownId
        ? "You requested this transfer (server attribution)."
        : `Requesting officer ${record.created_by.slice(0, 8)} (server attribution).`;

  function reloadRecord() {
    // Explicit reload flow: refetch the record (its snapshot
    // drifted under the lock set, its status moved, or the version
    // advanced); the stale decision is NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["shares", "transfer", transferId] });
    setReloadEpoch((epoch) => epoch + 1);
    approve.reset();
    reject.reset();
    setNotice(
      "Record reloaded — nothing was posted. If the transferor's shares drifted since the request, reject this transfer and raise a fresh one to capture current state.",
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
      title="Share transfer"
      onClose={onClose}
      closeDisabled={anyPending}
      // This drawer bears money-write affordances — a stray overlay
      // click must never discard an in-progress decision (W56-3).
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Status">{transferStatusPill(record.status)}</Kv>
        <Kv label="Transfer id">
          <span className={styles.mono}>{record.id}</span>
        </Kv>
        <Kv label="From member">
          <ShortId id={record.from_member_id} />
        </Kv>
        <Kv label="To member">
          <ShortId id={record.to_member_id} />
        </Kv>
        <Kv label="Amount">
          <span className={styles.amountCell}>{fmtKes(record.amount)}</span>
        </Kv>
        <Kv label="Maker">
          {record.created_by !== null ? <ShortId id={record.created_by} /> : "— (pre-workflow)"}
        </Kv>
        <Kv label="Checker">
          {record.approved_by !== null ? <ShortId id={record.approved_by} /> : "— (undecided)"}
        </Kv>
        {record.out_transaction_id !== null && (
          <Kv label="OUT ledger row">
            <ShortId id={record.out_transaction_id} />
          </Kv>
        )}
        {record.in_transaction_id !== null && (
          <Kv label="IN ledger row">
            <ShortId id={record.in_transaction_id} />
          </Kv>
        )}
        <Kv label="Requested">{fmtDateTime(record.created_at)}</Kv>
        <Kv label="Decided">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Record version">{record.version}</Kv>
      </div>
      <div className={styles.formNote}>
        Every figure above is the SERVER&apos;s, rendered verbatim. The
        record deliberately carries NO balance snapshot (least disclosure)
        — approval re-verifies the persisted snapshot component-by-component
        under the full lock set server-side and returns a conflict on any
        drift, posting nothing.
      </div>

      {/* One copy of the 409 reload flow (reuse-first). */}
      <ConflictBanner error={approve.error} onReload={reloadRecord} />
      {approve.isError && !approveConflict && <ErrorBanner error={approve.error} />}
      <ConflictBanner error={reject.error} onReload={reloadRecord} />
      {reject.isError && !rejectConflict && <ErrorBanner error={reject.error} />}

      {approveResult !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Transfer posted · {approveResult.out_txn_ref} / {approveResult.in_txn_ref}
          </div>
          <Kv label="Amount transferred">
            <span className={styles.amountCell}>{fmtKes(approveResult.amount)}</span>
          </Kv>
          <Kv label="Transferor shares after">
            <span className={styles.amountCell}>{fmtKes(approveResult.from_balance_after)}</span>
          </Kv>
          <Kv label="Transferee shares after">
            <span className={styles.amountCell}>{fmtKes(approveResult.to_balance_after)}</span>
          </Kv>
          <div className={styles.formNote}>
            Every figure above came from the approval response — the two
            balances belong to two different members and nothing is summed
            or reconciled in this screen. BOTH members are notified via the
            outbox. The ledger is append-only; a mistaken transfer is
            corrected by a counter-transfer, never an edit.
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
          This transfer is {record.status === "posted" ? "posted" : "rejected"} — a terminal
          state. No further checker action exists for it (the server and
          the 0040 status-machine trigger enforce this regardless).
        </div>
      )}

      {!terminal && approveResult === null && record.created_by !== null && (
        <>
          <div className={styles.subhead}>Checker decision</div>
          <MakerCheckerPanel
            makerId={record.created_by}
            makerLabel={makerLabel}
            makerAt={fmtDateTime(record.created_at)}
            checkerActions={
              mayApprove ? (
                <>
                  <div className={styles.formNote}>
                    Approving re-verifies the persisted snapshot under the
                    full lock set, posts BOTH ledger legs and notifies BOTH
                    members. The maker can never check their own request and
                    assurance roles can never be checker (both
                    server-enforced; the maker≠checker rule is also a
                    database CHECK).
                  </div>
                  <FormField
                    id="transfer-reject-reason"
                    label="Rejection rationale (required to reject)"
                    error={rejectReasonError === "" ? undefined : rejectReasonError}
                    hint="Four-eyes practice: the checker's reason goes on the audit record; no money moves on a rejection."
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
                    <Button
                      type="button"
                      variant="danger"
                      onClick={armReject}
                      disabled={anyPending}
                    >
                      Reject transfer…
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
                      Approve & post transfer…
                    </Button>
                  </div>
                </>
              ) : (
                <div className={styles.formNote}>
                  Your role has no members approval permission — checker
                  controls are not offered. The server enforces this
                  regardless.
                </div>
              )
            }
          >
            <div className={styles.formNote}>
              Pending transfer of {fmtKes(record.amount)} from member{" "}
              {record.from_member_id.slice(0, 8)} to member{" "}
              {record.to_member_id.slice(0, 8)} (server snapshot, re-verified
              component-by-component at approval).
            </div>
          </MakerCheckerPanel>
        </>
      )}

      {confirmApprove && (
        <ConfirmDangerModal
          title="Approve share transfer"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Approve & post transfer"
          pending={approve.isPending}
          onConfirm={() => {
            if (!approve.isPending) approve.mutate();
          }}
          onClose={() => setConfirmApprove(false)}
        >
          <Banner>
            This moves {fmtKes(record.amount)} of share capital from member{" "}
            {record.from_member_id.slice(0, 8)} to member{" "}
            {record.to_member_id.slice(0, 8)} — both ledger legs post
            atomically and BOTH members are notified. The server re-verifies
            the persisted snapshot under the full lock set — any drift is a
            conflict with NOTHING posted. The wire body is empty by
            contract: every figure comes from the pending record.
          </Banner>
        </ConfirmDangerModal>
      )}

      {confirmReject && (
        <ConfirmDangerModal
          title="Reject share transfer"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Reject transfer"
          pending={reject.isPending}
          onConfirm={() => {
            if (!reject.isPending) reject.mutate(rejectReason.trim());
          }}
          onClose={() => setConfirmReject(false)}
        >
          <Banner>
            This rejects the pending transfer with your rationale on the
            record. No money moves; the rejected record is terminal
            write-once workflow history. The write pins record version{" "}
            {record.version} — any drift is a conflict with nothing changed.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
