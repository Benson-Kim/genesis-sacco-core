"use client";

/**
 * Recovery-receipt dialog (the issue- money-in path against a POSTED write-off's surviving claim): `POST
 * /corrections/write-offs/{id}/recoveries` (corrections:create, RC-
 * ref) — never a resurrection of the loan.
 *
 * - The dialog fetches the write-off FRESH (record class, staleTime 0)
 *   and offers the action ONLY while the status is `posted` — the UI
 *   never offers what the API forbids (least disclosure).
 * - MONEY (blocker (a)): the amount is the typed decimal STRING
 *   end-to-end — the cash physically received, the ONE caller-known
 *   figure on this module. The claim figures (recovered total,
 *   outstanding) are server-resolved under the write-off row lock; an
 *   over-recovery is a 409, refused with nothing posted. Every result
 *   figure renders VERBATIM from the response.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   write-off id prefix), pending short-circuit, `retry: 0`, one
 *   Idempotency-Key per logical intent — money-mover material (README
 *   rule 4): canonical body + fresh record version + reload epoch + a
 *   per-SUCCESS intent counter (partial recoveries legitimately repeat
 *   — an identical second receipt after "Record another" is a NEW
 *   intent, never served the stored response).
 * - A 409 (over-recovery, or the claim moved underneath) renders the
 *   shared ConflictBanner's explicit reload-and-re-enter flow —
 *   NOTHING is replayed.
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { CASH_CHANNELS, CHANNEL_LABELS } from "@/modules/transactions/schemas";
import { fetchWriteOff, recordRecoveryReceipt } from "../api";
import {
  receiptAmountEntrySchema,
  type ReceiptAmountEntry,
  type RecoveryReceiptResult,
} from "../schemas";
import styles from "./Corrections.module.css";

export function RecoveryReceiptDialog({
  writeOffId,
  onClose,
}: Readonly<{
  writeOffId: string;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState("");
  const [channel, setChannel] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<ReceiptAmountEntry | null>(null);
  const [result, setResult] = useState<RecoveryReceiptResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-receipt intent counter (the lesson): partial recoveries
  // legitimately repeat — bumped on every SUCCESS so an identical
  // second receipt is a NEW intent with a NEW key.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Record class (staleTime 0): the fresh claim state gates arming.
  const detail = useQuery({
    queryKey: ["corrections", "write-off", writeOffId],
    queryFn: () => fetchWriteOff(writeOffId),
    staleTime: STALE_TIME.record,
  });

  const record = useMutation({
    // STRUCTURAL freshness guard: the money write refuses to
    // fire without the loaded fresh record.
    mutationFn: (entry: ReceiptAmountEntry) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh write-off record is not loaded — the receipt was NOT recorded."),
        );
      }
      return recordRecoveryReceipt(
        writeOffId,
        entry.amount,
        entry.channel,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "recovery-receipt",
            id: writeOffId,
            amount: entry.amount,
            channel: entry.channel,
            recordVersion: fresh.version,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      );
    },
    onSuccess: (posted) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form.
      setResult(posted);
      // The NEXT receipt is a new intent even if byte-identical (T2).
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce(
        posted.claim_fully_recovered
          ? "Recovery receipt posted — the claim is fully recovered; guarantees released."
          : "Recovery receipt posted against the surviving claim.",
      );
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-off", writeOffId] });
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The recovery receipt was NOT recorded.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Record recovery receipt" onClose={onClose} variant="dialog" dismissOnOverlay={false}>
        <div className={styles.muted}>Loading write-off record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Record recovery receipt" onClose={onClose} variant="dialog" dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const writeOff = detail.data;
  const conflict = record.isError && isConflict(record.error);
  const spent = result !== null;
  const recordable = writeOff.status === "posted";

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the claim (another
    // receipt landed underneath, or the amount exceeds the outstanding
    // claim); the failed receipt is NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["corrections", "write-off", writeOffId] });
    void queryClient.invalidateQueries({
      queryKey: ["corrections", "write-off", writeOffId, "receipts"],
    });
    setReloadEpoch((epoch) => epoch + 1);
    record.reset();
    setNotice(
      "Record reloaded — nothing was posted. The receipt may exceed the outstanding claim (the server never collects more than the claim); re-check the recovery trail before re-entering.",
    );
    announce("Record reloaded after the conflict — nothing was posted.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (record.isPending || spent) return;
    const parsed = receiptAmountEntrySchema.safeParse({
      amount: amount.trim(),
      channel,
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    setConfirmEntry(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(record.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    record.error instanceof ApiError &&
    record.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <Modal
      title="Record recovery receipt"
      onClose={onClose}
      closeDisabled={record.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never discard the half-armed
      // money write.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Write-off">
          <span className={styles.mono} title={writeOff.id}>
            {writeOff.id.slice(0, 8)}
          </span>
        </Kv>
        <Kv label="Status">{writeOff.status}</Kv>
        <Kv label="Total written off (claim)">
          <span className={styles.netCell}>{fmtKes(writeOff.total_written_off)}</span>
        </Kv>
        <Kv label="Pinned record version">{writeOff.version}</Kv>
      </div>

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={record.error} onReload={reloadAfterConflict} />
      {record.isError && !conflict && !renderedInline && <ErrorBanner error={record.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Receipt posted · ref {result.txn_ref}</div>
          <Kv label="Amount received">
            <span className={styles.amountCell}>{fmtKes(result.amount)}</span>
          </Kv>
          <Kv label="Recovered so far">
            <span className={styles.amountCell}>{fmtKes(result.recovered_total)}</span>
          </Kv>
          <Kv label="Outstanding claim">
            <span className={styles.netCell}>{fmtKes(result.outstanding_claim)}</span>
          </Kv>
          <Kv label="Claim fully recovered">{result.claim_fully_recovered ? "Yes" : "No"}</Kv>
          <Kv label="Guarantees released">{result.guarantees_released}</Kv>
          {result.recovery_case_id !== null && (
            <Kv label="Recovery case">
              <span className={styles.mono} title={result.recovery_case_id}>
                {result.recovery_case_id.slice(0, 8)}
              </span>
            </Kv>
          )}
          <Kv label="RC- ledger row">
            <span className={styles.mono}>{result.txn_id}</span>
          </Kv>
          <div className={styles.formNote}>
            The position above is the SERVER&apos;s from the receipt response
            — nothing was computed in this screen. The loan is never
            resurrected: recovery is income, not a repayment.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            {!result.claim_fully_recovered && (
              <Button
                type="button"
                variant="primary"
                onClick={() => {
                  // A NEW intent: entry cleared; fresh key material by
                  // content + intent_seq.
                  setResult(null);
                  setAmount("");
                  setChannel("");
                  setClientErrors({});
                  record.reset();
                }}
              >
                Record another
              </Button>
            )}
          </div>
        </div>
      )}

      {!spent && recordable && (
        <form onSubmit={submitEntry} noValidate>
          <FormField
            id="receipt-amount"
            label="Amount received (KES)"
            error={fieldErrors["amount"]}
            hint="The cash actually received — the server refuses any receipt exceeding the outstanding claim."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={18}
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                disabled={record.isPending}
              />
            )}
          </FormField>
          <FormField id="receipt-channel" label="Channel" error={fieldErrors["channel"]}>
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={channel}
                onChange={(event) => setChannel(event.target.value)}
                disabled={record.isPending}
              >
                <option value="">Select a channel…</option>
                {CASH_CHANNELS.map((option) => (
                  <option key={option} value={option}>
                    {CHANNEL_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <div className={styles.formNote}>
            Money physically moves via M-Pesa or Bank only. The posting date
            and the resulting claim position are resolved by the server.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={record.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={record.isPending}>
              {record.isPending ? "Recording…" : "Record receipt…"}
            </Button>
          </div>
        </form>
      )}
      {!spent && !recordable && (
        <div className={styles.formNote}>
          This write-off is not posted — recovery receipts exist only against
          a posted claim (the server enforces this regardless).
        </div>
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Record recovery receipt"
          confirmPhrase={writeOff.id.slice(0, 8)}
          confirmLabel="Record receipt"
          pending={record.isPending}
          onConfirm={() => {
            if (!record.isPending) record.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This posts a bad-debt recovery of {fmtKes(confirmEntry.amount)} via{" "}
            {CHANNEL_LABELS[confirmEntry.channel]} against the surviving claim
            (total {fmtKes(writeOff.total_written_off)}, server snapshot). A
            receipt that clears the claim discharges the guarantors and
            unblocks the member&apos;s exit. The ledger is append-only.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
