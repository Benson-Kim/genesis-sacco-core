"use client";

/**
 * Request share-transfer modal (the MAKER phase, split out of the
 * plain screen into a Modal — reuse-first, the #35 item 14 lookup
 * pattern already shipped in PostTransactionDrawer/FeeDrawer):
 * `POST /members/{member_id}/share-transfers` (members:approve).
 * Creates a PENDING workflow record bound to the persisted approval
 * snapshot — NO money moves from this form.
 *
 * MEMBER IDENTIFICATION (the point of this drawer): both the
 * transferring and receiving member are resolved by an operator-facing
 * identifier — member number OR national ID number — via the SAME
 * exact-match lookups the teller posting drawer uses
 * (lookupMemberByNo / lookupMemberByIdNumber). The raw member UUID is
 * NEVER typed, NEVER displayed: it exists only as the resolved id
 * threaded internally into the write.
 *
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   transferring member's NUMBER, never a uuid), pending short-circuit,
 *   `retry: 0`, one Idempotency-Key per logical intent (money-mover
 *   rule 4: canonical entry + reload epoch + per-SUCCESS intent
 *   counter — a legitimate identical second request is a NEW intent).
 * - A 409 (insufficient balance under the locks, inactive member,
 *   self-transfer) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow — NOTHING is replayed.
 * - MONEY (blocker (a)): the amount is the typed decimal STRING
 *   end-to-end, never a float.
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import {
  Banner,
  Button,
  ConfirmDangerModal,
  Kv,
  Modal,
  FormField,
  fromApiError,
  fromZodError,
  mergeFieldErrors,
  type FieldErrors,
  ConflictBanner,
  ErrorBanner,
  announce,
  Input,
} from "@genesis/design-system";
import { fmtKes } from "@/lib/format";
import type { Member } from "@/modules/members/schemas";
import { isConflict } from "@/lib/errors";
import { MemberLookupField } from "@/modules/members/components/MemberLookupField";
import { requestShareTransfer } from "../api";
import { transferEntrySchema, type ShareTransferRecord } from "../schemas";
import { transferStatusPill } from "./pills";
import styles from "./Shares.module.css";

interface ResolvedEntry {
  from: Member;
  to: Member;
  amount: string;
}

export function RequestTransferDrawer({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [fromMember, setFromMember] = useState<Member | null>(null);
  const [toMember, setToMember] = useState<Member | null>(null);
  const [amount, setAmount] = useState("");
  // Bumped on "Record another request": forces both MemberLookupField
  // subtrees to remount so their OWN input/resolution state clears
  // alongside the parent's — otherwise a stale "Member verified" note
  // could linger for a member no longer selected (the fields would
  // still be inert — submission is gated on fromMember/toMember being
  // freshly re-resolved — but the display would mislead the operator).
  const [formKey, setFormKey] = useState(0);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<ResolvedEntry | null>(null);
  const [requested, setRequested] = useState<ShareTransferRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on every
  // explicit post-conflict reload — a re-entered identical request
  // after a 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success intent counter (the lesson): a SECOND request of the
  // same amount between the same members is legitimate and is a NEW
  // intent — never served the first request's stored response.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const request = useMutation({
    mutationFn: (entry: ResolvedEntry) =>
      requestShareTransfer(
        entry.from.id,
        entry.to.id,
        entry.amount,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "share-transfer-request",
            from: entry.from.id,
            to: entry.to.id,
            amount: entry.amount,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form.
      setRequested(record);
      // The NEXT request is a new intent even if byte-identical (T2).
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce("Transfer request recorded — PENDING a different checker's approval; no money moved.");
      void queryClient.invalidateQueries({ queryKey: ["shares", "transfers-register"] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The transfer request was NOT recorded.");
    },
  });

  const conflict = request.isError && isConflict(request.error);
  const spent = requested !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: the conflicted attempt recorded NOTHING
    // (server-verified under the full lock set); the operator re-checks
    // and re-enters. The stale intent is NEVER replayed: its key
    // rotates via the epoch.
    setReloadEpoch((epoch) => epoch + 1);
    request.reset();
    void queryClient.invalidateQueries({ queryKey: ["shares", "transfers-register"] });
    setNotice(
      "Nothing was recorded by the conflicted attempt. Re-check the transferring member's share balance and status before re-entering.",
    );
    announce("Conflict acknowledged — nothing was recorded; re-check the member before re-entering.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (request.isPending || spent) return;
    const parsed = transferEntrySchema.safeParse({
      from_member_id: fromMember?.id ?? "",
      to_member_id: toMember?.id ?? "",
      amount: amount.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    if (fromMember === null || toMember === null) {
      setClientErrors({ from_member_id: "Look up and select both members first." });
      return;
    }
    setClientErrors({});
    setConfirmEntry({ from: fromMember, to: toMember, amount: parsed.data.amount });
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
      title="Transfer shares"
      onClose={onClose}
      closeDisabled={request.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // money-adjacent request.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={request.error} onReload={reloadAfterConflict} />
      {request.isError && !conflict && !renderedInline && <ErrorBanner error={request.error} />}

      {spent && requested !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Transfer request recorded · PENDING approval
          </div>
          {/* States the maker-checker posture on screen, not just in the
              live-region announcement: NO money moved here — the equity
              only moves when a DIFFERENT principal approves. No balance
              figure exists to render at this point. */}
          <div className={styles.formNote}>
            NO money moved — this records a request only. The shares move when a
            different officer approves it.
          </div>
          <Kv label="From">
            {requested.from_member_no ?? "—"}
            {requested.from_member_name !== null ? ` · ${requested.from_member_name}` : ""}
          </Kv>
          <Kv label="To">
            {requested.to_member_no ?? "—"}
            {requested.to_member_name !== null ? ` · ${requested.to_member_name}` : ""}
          </Kv>
          <Kv label="Status">{transferStatusPill(requested.status)}</Kv>
          <Kv label="Amount requested">
            <span className={styles.amountCell}>{fmtKes(requested.amount)}</span>
          </Kv>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                // A NEW intent: entry cleared; fresh key material by
                // content + intent_seq.
                setRequested(null);
                setFromMember(null);
                setToMember(null);
                setAmount("");
                setClientErrors({});
                setFormKey((key) => key + 1);
                request.reset();
              }}
            >
              Record another request
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <MemberLookupField
            key={`from-${formKey}`}
            idPrefix="transfer-from"
            legLabel="Transferring member (shares move OUT)"
            hint=""
            disabled={request.isPending}
            onResolved={setFromMember}
          />
          {fieldErrors["from_member_id"] !== undefined && (
            <div className={styles.formNote} role="alert">
              {fieldErrors["from_member_id"]}
            </div>
          )}
          <MemberLookupField
            key={`to-${formKey}`}
            idPrefix="transfer-to"
            legLabel="Receiving member (shares move IN)"
            hint=""
            disabled={request.isPending}
            onResolved={setToMember}
          />
          {fieldErrors["to_member_id"] !== undefined && (
            <div className={styles.formNote} role="alert">
              {fieldErrors["to_member_id"]}
            </div>
          )}
          <FormField
            id="transfer-amount"
            label="Amount (KES)"
            error={fieldErrors["amount"]}
            hint="The share capital to move"
          >
            {(control) => (
              <Input
                {...control}
                inputMode="decimal"
                maxLength={18}
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                disabled={request.isPending}
              />
            )}
          </FormField>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={request.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={request.isPending}>
              {request.isPending ? "Recording…" : "Request transfer…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Request share transfer"
          confirmPhrase={confirmEntry.from.member_no}
          confirmLabel="Request transfer"
          pending={request.isPending}
          onConfirm={() => {
            if (!request.isPending) request.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This records a PENDING request to move {fmtKes(confirmEntry.amount)}{" "}
            of share capital from {confirmEntry.from.name} · {confirmEntry.from.member_no} to{" "}
            {confirmEntry.to.name} · {confirmEntry.to.member_no}.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
