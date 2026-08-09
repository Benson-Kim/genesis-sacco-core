"use client";

/**
 * Loan write-off request drawer (the MAKER phase of the committee write-off): `POST /corrections/write-offs`
 * (corrections:create) persists the write-once 0025 snapshot for
 * committee approval.
 *
 * - The body carries the loan id and the reason ONLY (v1.1 rules 1/3):
 *   the snapshot figures (balance, penalty due, total written off,
 *   classification, provision) are read under the loan row lock
 *   server-side. The DB backstop makes a snapshot of a PERFORMING loan
 *   unrepresentable (0025 classification CHECK).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   loan id prefix), pending short-circuit, `retry: 0`, one
 *   Idempotency-Key per logical intent — simple-create material
 *   (README rule 2; the server holds the one-live-write-off-per-loan
 *   claim via the 0025 partial-unique index).
 * - SoD WITNESS: on success the requester is recorded in the per-tab
 *   makerRegistry — WriteOffOut does not serialise requested_by
 *   (contract follow-up on), so this witness is the ONLY
 *   maker signal for the vote/post SoD affordances; the server bans
 *   requester self-votes/postings regardless (least disclosure).
 * - The result panel renders the SERVER's snapshot verbatim (blocker
 *   (a)); the affordance is then SPENT.
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import {
  fromApiError,
  fromZodError,
  mergeFieldErrors,
  type FieldErrors,
} from "@/modules/forms/form-errors";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { loanClassPill } from "@/modules/loans/components/pills";
import { requestWriteOff } from "../api";
import { recordWriteOffMaker } from "../makerRegistry";
import {
  writeOffRequestEntrySchema,
  type WriteOffRecord,
  type WriteOffRequestEntry,
} from "../schemas";
import { writeOffStatusPill } from "./pills";
import styles from "./Corrections.module.css";

export function WriteOffRequestDrawer({
  onReview,
  onClose,
}: Readonly<{
  /** Open the created record in the write-off drawer (committee handoff). */
  onReview: (writeOffId: string) => void;
  onClose: () => void;
}>) {
  const [loanId, setLoanId] = useState("");
  const [reason, setReason] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<WriteOffRequestEntry | null>(null);
  const [result, setResult] = useState<WriteOffRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const queryClient = useQueryClient();

  const request = useMutation({
      mutationFn: (entry: WriteOffRequestEntry) =>
      requestWriteOff(
        entry.loan_id,
        entry.reason,
        idempotencyKeyFor(
          keySlot.current,
          // Simple-create material (README rule 2): op + the full
          // canonical body; the 0025 partial-unique index is the
          // one-live-write-off-per-loan authority.
          JSON.stringify({
            op: "write-off-request",
            loan_id: entry.loan_id,
            reason: entry.reason,
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form…
      setResult(record);
      setNotice("");
      announce("Write-off requested — awaiting committee votes.");
      // …and this tab witnessed the requester for the SoD panel (the
      // contract does not serialise requested_by — the registry is the
      // only witness; nothing is invented for other tabs).
      const ownId = getOwnUserId();
      if (ownId !== null) recordWriteOffMaker(record.id, ownId);
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-offs-register"] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The write-off was NOT requested.");
    },
  });

  const conflict = request.isError && isConflict(request.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: the conflicted request is
    // structurally WITHDRAWN — a live write-off already claims this
    // loan, or the loan is not write-off eligible. NOTHING is replayed.
    setReloadEpoch((epoch) => epoch + 1);
    request.reset();
    setNotice(
      "Nothing was requested — this loan may already carry a live write-off workflow (one per loan), or it is not in a write-off-eligible state. Look the record up by its id to review it.",
    );
    announce("Nothing was requested — re-check the loan before re-entering.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (request.isPending || spent) return;
    const parsed = writeOffRequestEntrySchema.safeParse({
      loan_id: loanId.trim(),
      reason: reason.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    setConfirmEntry(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(request.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    request.error instanceof ApiError &&
    request.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <Modal
      title="Request loan write-off"
      onClose={onClose}
      closeDisabled={request.isPending}
      // W56-3: a stray overlay click must never discard the
      // half-completed prudential entry.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={request.error} onReload={reloadAfterConflict} />
      {request.isError && !conflict && !renderedInline && <ErrorBanner error={request.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Write-off requested · awaiting committee votes
          </div>
          <Kv label="Status">{writeOffStatusPill(result.status)}</Kv>
          <Kv label="Write-off id">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Classification">{loanClassPill(result.classification)}</Kv>
          <Kv label="Balance (receivable)">
            <span className={styles.amountCell}>{fmtKes(result.balance)}</span>
          </Kv>
          <Kv label="Penalty due">
            <span className={styles.amountCell}>{fmtKes(result.penalty_due)}</span>
          </Kv>
          <Kv label="Total written off (surviving claim)">
            <span className={styles.netCell}>{fmtKes(result.total_written_off)}</span>
          </Kv>
          <Kv label="Provision">{result.provision_pct}%</Kv>
          <Kv label="Requested">{fmtDateTime(result.created_at)}</Kv>
          <div className={styles.formNote}>
            The snapshot above is the SERVER&apos;s, read under the loan row
            lock and write-once at the database — nothing was computed in
            this screen. Nothing has posted: the committee quorum decides —
            the request now leads the write-off committee register (no pending-write-offs
            register exists on the contract).
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button type="button" variant="primary" onClick={() => onReview(result.id)}>
              Open record
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <div className={styles.formNote}>
            Write-off is the LAST stage of credit deterioration and is NOT
            forgiveness: the receivable is derecognised but the legal claim on
            the member survives for recovery. Only a non-performing loan
            (substandard / doubtful / loss) can be snapshotted — the database
            refuses anything else.
          </div>
          <FormField
            id="woff-loan-id"
            label="Loan id"
            error={fieldErrors["loan_id"]}
            hint="From the Loan book register (the loan detail drawer shows the id)."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                value={loanId}
                onChange={(event) => setLoanId(event.target.value)}
                disabled={request.isPending}
                spellCheck={false}
              />
            )}
          </FormField>
          <FormField
            id="woff-reason"
            label="Reason"
            error={fieldErrors["reason"]}
            hint="Recorded immutably on the write-once snapshot and the audit trail."
          >
            {(control) => (
              <textarea
                {...control}
                className={styles.textarea}
                maxLength={500}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                disabled={request.isPending}
              />
            )}
          </FormField>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={request.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={request.isPending}>
              {request.isPending ? "Requesting…" : "Request write-off…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Request loan write-off"
          confirmPhrase={confirmEntry.loan_id.slice(0, 8)}
          confirmLabel="Request write-off"
          pending={request.isPending}
          onConfirm={() => {
            if (!request.isPending) request.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This snapshots loan {confirmEntry.loan_id.slice(0, 8)} for a
            committee write-off decision. The snapshot is write-once at the
            database; you will not be able to vote on or post your own request
            (separation of duties, server-enforced).
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
