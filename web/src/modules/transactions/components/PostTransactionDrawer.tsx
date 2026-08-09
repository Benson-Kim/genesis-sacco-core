"use client";

/**
 * Post-transaction drawer (prototype `txnModal`,
 * adapted to the P11 contract): the teller money writes this batch
 * owns — deposit, withdrawal, share top-up
 * (`POST /members/{id}/deposits | /withdrawals | /share-topups`,
 * transactions:create). Loan repayments ship with the Loan book batch
 * and fees with the corrections module — the prototype
 * options that map there are deliberately NOT duplicated here.
 *
 * - FRESH MEMBER READ before every write (record class, staleTime 0;
 *   pattern (e)): the drawer only arms the confirmation once the
 *   member record has been read fresh — an exited member is withdrawn
 *   structurally (the server 409s regardless). Double-posting from a
 *   stale read is THE failure mode of this module.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   member number), pending short-circuit + disabled controls,
 *   `retry: 0`, one Idempotency-Key per logical intent. The key
 *   material folds in the fresh member VERSION and a reload epoch
 *  : stable across pure retries of one intent, rotated when
 *   the kind, member, amount, channel, the reloaded record version OR
 *   an explicit post-409 reload changes the intent.
 * - A 409 (insufficient available balance under the account row lock —
 *   capacity excludes live guarantee pledges — or an exited member)
 *   renders the shared ConflictBanner's explicit reload-and-re-enter
 *   flow: reload refetches the member and the register; the failed
 *   posting is NEVER replayed (informational notice variant +
 *   announce()).
 * - MONEY (blocker (a)): the amount is the typed decimal STRING
 *   end-to-end (leading zeros rejected); the response amount
 *   and balance_after are SERVER facts rendered verbatim.
 * - Cash channels ONLY: the P11 contract rejects accrual/internal on
 *   teller writes (require_cash_channel) — the UI never offers them.
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, fromZodError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember, lookupMemberByNo } from "@/modules/members/api";
import { postMoneyWrite } from "../api";
import {
  CASH_CHANNELS,
  CHANNEL_LABELS,
  TXN_WRITE_KINDS,
  TXN_WRITE_KIND_LABELS,
  moneyEntrySchema,
  type AccountTxn,
  type MoneyEntryInput,
} from "../schemas";
import styles from "./Transactions.module.css";

/** Operator-facing wording per write kind (server semantics, P11). */
const KIND_EFFECT: Record<MoneyEntryInput["kind"], string> = {
  deposit: "credits the member's deposit account",
  withdrawal:
    "debits the member's deposit account (the server verifies the available balance — net of live guarantee pledges — under the account row lock)",
  share_topup: "credits the member's share capital",
};

export function PostTransactionDrawer({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("deposit");
  // The member is looked up by their UNIQUE number on
  // blur — the full member-list select no longer exists here. The
  // lookup resolves ONLY the blurred value; editing the field
  // withdraws the resolution until the next blur.
  const [memberNoInput, setMemberNoInput] = useState("");
  const [lookupNo, setLookupNo] = useState("");
  const [amount, setAmount] = useState("");
  const [channel, setChannel] = useState("");
  // External receipt reference: required by the server on
  // both offered channels; cleared per posting so a duplicate ref can
  // never ride a "Post another" flow by accident.
  const [externalRef, setExternalRef] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<MoneyEntryInput | null>(null);
  const [result, setResult] = useState<AccountTxn | null>(null);
  const [resultKind, setResultKind] = useState<MoneyEntryInput["kind"]>("deposit");
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload, so a re-entered identical
  // entry after a 409 is a NEW intent with a NEW key — never a replay
  // served from the backend idempotency store's pinned first outcome.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-posting intent counter (review T2): bumped on every SUCCESS, so
  // a teller re-entering an IDENTICAL entry after "Post another" (two
  // equal cash deposits the same day — a routine SACCO flow) is a NEW
  // intent with a NEW key. Without it the server would dedup the second
  // posting and silently omit it from the ledger while showing success.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Unique-identifier lookup: the expand-only member_no
  // exact-match param on GET /members (DB UNIQUE (tenant_id,
  // member_no)). No status filter: deposits into arrears or dormant
  // accounts are legitimate teller flows; the server rejects exited
  // members regardless.
  const lookup = useQuery({
    queryKey: ["members", "lookup", lookupNo === "" ? "none" : lookupNo],
    queryFn: () => lookupMemberByNo(lookupNo),
    enabled: lookupNo !== "",
    staleTime: STALE_TIME.record,
  });
  const memberId = lookup.data?.id ?? "";

  // FRESH record read (staleTime 0) the moment a member is chosen —
  // the confirmation only arms once this read has landed (pattern (e)).
  const memberDetail = useQuery({
    queryKey: ["members", "detail", memberId === "" ? "none" : memberId],
    queryFn: () => fetchMember(memberId),
    enabled: memberId !== "",
    staleTime: STALE_TIME.record,
  });
  const freshMember = memberDetail.data;

  const post = useMutation({
    mutationFn: (input: MoneyEntryInput) =>
      postMoneyWrite(
        input.kind,
        input.member_id,
        { amount: input.amount, channel: input.channel, external_ref: input.external_ref },
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "txn-post",
            input,
            member_version: freshMember?.version ?? null,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (recorded, input) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form; posting
      // again requires "Post another", which clears the entry (a NEW
      // intent — an identical resubmission would reuse the same key and
      // be deduplicated server-side rather than double-posting).
      setResult(recorded);
      setResultKind(input.kind);
      // The NEXT posting is a new intent even if byte-identical (T2).
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce(`${TXN_WRITE_KIND_LABELS[input.kind]} posted to the ledger.`);
      // The register and the member's balances are server facts — refetch.
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["members", "detail", input.member_id] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirmEntry(null);
      announce("The transaction was NOT posted.");
    },
  });

  const conflict = post.isError && isConflict(post.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the member (their balance
    // or status moved underneath) and the register; the failed posting
    // is structurally WITHDRAWN — re-entering it is a NEW operator
    // intent whose key rotates via the reload epoch. NOTHING
    // is replayed.
    void queryClient.refetchQueries({ queryKey: ["members", "detail", memberId] });
    void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    post.reset();
    setNotice(
      "Record reloaded — the conflicted posting was withdrawn, nothing was replayed. Re-check the member before re-entering.",
    );
    announce("Record reloaded. The conflicted posting was withdrawn; nothing was replayed.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (post.isPending || spent) return;
    const parsed = moneyEntrySchema.safeParse({
      kind,
      member_id: memberId,
      amount: amount.trim(),
      channel,
      external_ref: externalRef.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    // The confirmation arms ONLY on a fresh member read (pattern (e)).
    if (freshMember === undefined) {
      setClientErrors({ member_id: "The member record is still being verified — wait a moment." });
      return;
    }
    setClientErrors({});
    setConfirmEntry(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(post.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    post.error instanceof ApiError &&
    post.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  // Structural withdrawal: the server rejects every teller posting for
  // an exited member — the form is not offered (least disclosure).
  const memberExited = freshMember !== undefined && freshMember.status === "exited";

  return (
    <Modal
      title="Post transaction"
      onClose={onClose}
      closeDisabled={post.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // teller posting — dismissal is the explicit ✕ or Escape. (This
      // module was written before the focus-dismissal sweep and inherited the
      // mechanism via a later main-merge; convention completed here.)
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={post.error} onReload={reloadAfterConflict} />
      {post.isError && !conflict && !renderedInline && <ErrorBanner error={post.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            {TXN_WRITE_KIND_LABELS[resultKind]} posted · ref {result.txn_ref}
          </div>
          <Kv label="Amount">{fmtKes(result.amount)}</Kv>
          <Kv label="Balance after">{fmtKes(result.balance_after)}</Kv>
          <Kv label="Ledger row">
            <span className={styles.mono}>{result.txn_id}</span>
          </Kv>
          <div className={styles.formNote}>
            The balance above is the SERVER&apos;s figure from the posting
            response — nothing was computed in this screen. The row now
            appears in the register.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                // A NEW intent: entry cleared; the next submission builds
                // fresh key material by content.
                setResult(null);
                setAmount("");
                setChannel("");
                setExternalRef("");
                setClientErrors({});
                post.reset();
              }}
            >
              Post another
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <FormField id="txn-kind" label="Type" error={fieldErrors["kind"]}>
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={kind}
                onChange={(event) => setKind(event.target.value)}
                disabled={post.isPending}
              >
                {TXN_WRITE_KINDS.map((option) => (
                  <option key={option} value={option}>
                    {TXN_WRITE_KIND_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <div className={styles.formNote}>
            Loan repayments are recorded from the Loan book (the loan detail
            drawer); misc fees belong to the corrections module — neither is
            posted from this screen.
          </div>
          <FormField
            id="txn-member-no"
            label="Member number"
            error={fieldErrors["member_id"]}
            hint="Unique member number (e.g. GP-0421) — looked up when you leave the field."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="search"
                maxLength={32}
                value={memberNoInput}
                onChange={(event) => {
                  setMemberNoInput(event.target.value);
                  // Editing withdraws any prior resolution: the money
                  // write can only ever target the value the operator
                  // actually blurred.
                  setLookupNo("");
                }}
                onBlur={() => setLookupNo(memberNoInput.trim())}
                disabled={post.isPending}
              />
            )}
          </FormField>
          {lookupNo !== "" && lookup.isPending && (
            <div className={styles.formNote}>Looking up member {lookupNo}…</div>
          )}
          {lookupNo !== "" && lookup.isError && <ErrorBanner error={lookup.error} />}
          {lookupNo !== "" && !lookup.isPending && !lookup.isError && lookup.data === null && (
            <div className={styles.formNote} role="status">
              No member found with number {lookupNo} — check the number and try again.
            </div>
          )}
          {memberId !== "" && memberDetail.isPending && (
            <div className={styles.formNote}>Verifying the member record…</div>
          )}
          {memberId !== "" && memberDetail.isError && <ErrorBanner error={memberDetail.error} />}
          {freshMember !== undefined && !memberExited && (
            <div className={styles.formNote}>
              Member verified: {freshMember.name} · {freshMember.member_no} (record
              version {freshMember.version}) — read fresh before this posting.
            </div>
          )}
          {memberExited && (
            <Banner variant="error">
              This member has EXITED — the ledger accepts no further teller
              postings for them (the server enforces this regardless).
            </Banner>
          )}
          <FormField
            id="txn-amount"
            label="Amount (KES)"
            error={fieldErrors["amount"]}
            hint="Decimal amount, e.g. 5000 or 5000.50 — no leading zeros. The resulting balance comes from the server."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={18}
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                disabled={post.isPending}
              />
            )}
          </FormField>
          <FormField id="txn-channel" label="Channel" error={fieldErrors["channel"]}>
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={channel}
                onChange={(event) => setChannel(event.target.value)}
                disabled={post.isPending}
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
          <FormField
            id="txn-ext-ref"
            label="External reference"
            error={fieldErrors["external_ref"]}
            hint="M-Pesa confirmation code or bank slip reference — required; the server refuses a duplicate per channel."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={40}
                value={externalRef}
                onChange={(event) => setExternalRef(event.target.value)}
                disabled={post.isPending}
              />
            )}
          </FormField>
          <div className={styles.formNote}>
            Money physically moves via M-Pesa or Bank only — accrual and
            internal postings are made by server jobs and are not offered
            here. The posting date is resolved by the server.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={post.isPending}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={post.isPending || memberExited}
            >
              {post.isPending ? "Posting…" : "Post to ledger…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && freshMember !== undefined && (
        <ConfirmDangerModal
          title="Post to ledger"
          confirmPhrase={freshMember.member_no}
          confirmLabel="Post to ledger"
          pending={post.isPending}
          onConfirm={() => {
            if (!post.isPending) post.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This posts a {TXN_WRITE_KIND_LABELS[confirmEntry.kind]} of{" "}
            {fmtKes(confirmEntry.amount)} via {CHANNEL_LABELS[confirmEntry.channel]} for{" "}
            {freshMember.name} · {freshMember.member_no}. It {KIND_EFFECT[confirmEntry.kind]}. The
            ledger is append-only — undoing this requires a correction posting.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
