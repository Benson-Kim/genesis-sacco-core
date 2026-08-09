"use client";

/**
 * Repayment-adjustment request drawer (the MAKER phase of the issue- two-phase correction): `POST
 * /corrections/repayment-adjustments` (corrections:create) creates a
 * PENDING adjustment bound to the persisted approval snapshot —
 * nothing posts until a DISTINCT checker approves.
 *
 * - The body carries the repayment id and the reason ONLY — NO amounts,
 *   ever (rule 1): the reversal derives every figure from the
 *   original transaction's append-only legs. There is no repayments
 *   list contract to pick from (the id comes from the posting receipt
 *   or the audit trail — recorded honestly on the workbench card).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   repayment id prefix), pending short-circuit, `retry: 0`, one
 *   Idempotency-Key per logical intent — simple-create material (the README rule 2: op + the full canonical body; the server holds the one-live-adjustment-per-repayment claim).
 * - A 409 (a live adjustment already exists / the repayment is not
 *   adjustable) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow — NOTHING is replayed; the re-entered
 *   intent rotates the key via the reload epoch.
 * - The result panel renders the SERVER's snapshot verbatim (blocker
 *   (a)): the complete original allocation being undone and the 0031
 *   approval snapshot. The affordance is then SPENT.
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
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { requestAdjustment } from "../api";
import {
  adjustmentRequestEntrySchema,
  type AdjustmentRecord,
  type AdjustmentRequestEntry,
} from "../schemas";
import { adjustmentStatusPill } from "./pills";
import styles from "./Corrections.module.css";

export function AdjustmentRequestDrawer({
  onReview,
  onClose,
}: Readonly<{
  /** Open the created record in the review drawer (checker handoff). */
  onReview: (adjustmentId: string) => void;
  onClose: () => void;
}>) {
  const [repaymentId, setRepaymentId] = useState("");
  const [reason, setReason] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<AdjustmentRequestEntry | null>(null);
  const [result, setResult] = useState<AdjustmentRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload — a re-entered identical entry
  // after an acknowledged 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const queryClient = useQueryClient();

  const request = useMutation({
     mutationFn: (entry: AdjustmentRequestEntry) =>
      requestAdjustment(
        entry.repayment_id,
        entry.reason,
        idempotencyKeyFor(
          keySlot.current,
          // Simple-create material (README rule 2): op + the full
          // canonical body; the server's one-live-adjustment claim is
          // the uniqueness authority. The reload epoch rotates the key
          // after an acknowledged conflict.
          JSON.stringify({
            op: "adjustment-request",
            repayment_id: entry.repayment_id,
            reason: entry.reason,
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form. The
      // maker is the CONTRACT's maker_id (server truth) — no per-tab
      // witness is needed on the adjustment surface.
      setResult(record);
      setNotice("");
      announce("Adjustment requested — awaiting a distinct checker.");
      void queryClient.invalidateQueries({ queryKey: ["corrections", "adjustments-register"] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The adjustment was NOT requested.");
    },
  });

  const conflict = request.isError && isConflict(request.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: the conflicted request is
    // structurally WITHDRAWN — a live adjustment already claims this
    // repayment, or the repayment is not adjustable. NOTHING is
    // replayed; re-entering is a NEW intent (rotated key).
    setReloadEpoch((epoch) => epoch + 1);
    request.reset();
    setNotice(
      "Nothing was requested — this repayment may already carry a live adjustment (one live adjustment per repayment). Look the pending record up by its id to review it.",
    );
    announce("Nothing was requested — re-check the repayment before re-entering.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (request.isPending || spent) return;
    const parsed = adjustmentRequestEntrySchema.safeParse({
      repayment_id: repaymentId.trim(),
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
      title="Request repayment adjustment"
      onClose={onClose}
      closeDisabled={request.isPending}
      // W56-3: a stray overlay click must never discard the
      // half-completed correction entry.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={request.error} onReload={reloadAfterConflict} />
      {request.isError && !conflict && !renderedInline && <ErrorBanner error={request.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Adjustment requested · awaiting a distinct checker
          </div>
          <Kv label="Status">{adjustmentStatusPill(result.status)}</Kv>
          <Kv label="Adjustment id">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Amount to reverse">
            <span className={styles.netCell}>{fmtKes(result.amount)}</span>
          </Kv>
          <Kv label="Penalties / interest / principal">
            <span className={styles.amountCell}>
              {fmtKes(result.penalties)} / {fmtKes(result.interest)} / {fmtKes(result.principal)}
            </span>
          </Kv>
          <Kv label="Would reopen the loan">{result.would_reopen_loan ? "Yes" : "No"}</Kv>
          <Kv label="Requested">{fmtDateTime(result.created_at)}</Kv>
          <div className={styles.formNote}>
            Every figure above derives from the original transaction&apos;s
            append-only legs, server-side — nothing was computed in this
            screen. Nothing has posted: a DIFFERENT checker must approve the
            persisted snapshot — the request now leads the pending-adjustments
            checker register.
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
            This reverses a repayment&apos;s COMPLETE allocation (penalties,
            interest and principal together — a partial-leg reversal is
            unrepresentable). No amount is entered here: every figure derives
            server-side from the original posting&apos;s ledger legs.
          </div>
          <FormField
            id="adj-repayment-id"
            label="Repayment id"
            error={fieldErrors["repayment_id"]}
            hint="The repayment row id from the posting receipt or the audit trail — the contract has no repayments register to pick from."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                value={repaymentId}
                onChange={(event) => setRepaymentId(event.target.value)}
                disabled={request.isPending}
                spellCheck={false}
              />
            )}
          </FormField>
          <FormField
            id="adj-reason"
            label="Reason"
            error={fieldErrors["reason"]}
            hint="Recorded immutably on the write-once adjustment row and the audit trail."
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
              {request.isPending ? "Requesting…" : "Request adjustment…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Request repayment adjustment"
          confirmPhrase={confirmEntry.repayment_id.slice(0, 8)}
          confirmLabel="Request adjustment"
          pending={request.isPending}
          onConfirm={() => {
            if (!request.isPending) request.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This opens a maker-checker correction against repayment{" "}
            {confirmEntry.repayment_id.slice(0, 8)}: the server snapshots the
            COMPLETE original allocation for a DIFFERENT checker to approve.
            You will not be able to approve it yourself (separation of
            duties, enforced server-side and at the database).
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
