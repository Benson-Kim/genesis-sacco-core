"use client";

/**
 * Misc-fee drawer (the prototype "Fee" posting the transactions module deliberately deferred to corrections):
 * `POST /corrections/fees` (corrections:create, FE- ref).
 *
 * - NO AMOUNT FIELD EXISTS (rule 1): the request names a
 *   CODE-OWNED fee type and the cash channel only — the amount
 *   resolves exclusively from tenant configuration server-side
 *   (extra="forbid" turns a caller-supplied amount into a 422). The
 *   result panel renders the SERVER's resolved figure verbatim.
 * - FRESH MEMBER READ before the write (record class, staleTime 0 —
 *   the PostTransactionDrawer pattern): the confirmation only arms
 *   once the member record has been read fresh; an exited member is
 *   withdrawn structurally (the server refuses regardless).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   member number), pending short-circuit, `retry: 0`, one
 *   Idempotency-Key per logical intent — money-mover material (README
 *   rule 4): the canonical body + fresh member version + reload epoch
 *   + a per-SUCCESS intent counter (a deliberately repeated fee after
 *   "Post another" is a NEW intent, never served the stored response).
 * - A 409 renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow — NOTHING is replayed.
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
import { useKeysetList } from "@/modules/table/useKeysetList";
import { fetchMember, fetchMembersPage } from "@/modules/members/api";
import type { Member } from "@/modules/members/schemas";
import {
  CASH_CHANNELS,
  CHANNEL_LABELS,
  cashChannelSchema,
} from "@/modules/transactions/schemas";
import { z } from "zod";
import { postFee } from "../api";
import { FEE_TYPES, FEE_TYPE_LABELS, feeTypeSchema, type FeeResult } from "../schemas";
import styles from "./Corrections.module.css";

/** Client-side pre-validation (the server re-validates — least disclosure):
 * member, code-owned fee type, cash channel. NO amount can even be
 * expressed in this input. */
const feeEntrySchema = z.object({
  member_id: z.string().min(1, "Select a member."),
  fee_type: feeTypeSchema,
  channel: cashChannelSchema,
});

type FeeEntry = z.infer<typeof feeEntrySchema>;

export function FeeDrawer({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [memberId, setMemberId] = useState("");
  const [feeType, setFeeType] = useState<string>("registration");
  const [channel, setChannel] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<FeeEntry | null>(null);
  const [result, setResult] = useState<FeeResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-posting intent counter (the lesson): bumped on every
  // SUCCESS, so a deliberately repeated identical fee after "Post
  // another" is a NEW intent with a NEW key — never deduplicated into
  // a silently missing ledger row.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Member picker: the keyset contract (scalability) — pages of 20 with
  // an explicit "load more" (the PostTransactionDrawer pattern; the
  // P8 list has no search parameter).
  const members = useKeysetList<Member>({
    queryKey: ["members", "list", { status: "", type: "" }],
    fetchPage: (cursor) => fetchMembersPage({ status: "", type: "" }, cursor),
  });
  const memberOptions = members.data?.pages.flatMap((page) => page.items) ?? [];

  // FRESH record read (staleTime 0) the moment a member is chosen —
  // the confirmation only arms once this read has landed.
  const memberDetail = useQuery({
    queryKey: ["members", "detail", memberId === "" ? "none" : memberId],
    queryFn: () => fetchMember(memberId),
    enabled: memberId !== "",
    staleTime: STALE_TIME.record,
  });
  const freshMember = memberDetail.data;

  const post = useMutation({
    mutationFn: (entry: FeeEntry) =>
      postFee(
        entry.member_id,
        entry.fee_type,
        entry.channel,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "fee-post",
            member_id: entry.member_id,
            fee_type: entry.fee_type,
            channel: entry.channel,
            member_version: freshMember?.version ?? null,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (posted, entry) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form.
      setResult(posted);
      // The NEXT posting is a new intent even if byte-identical (T2).
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce("Fee posted to the ledger.");
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["members", "detail", entry.member_id] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The fee was NOT posted.");
    },
  });

  const conflict = post.isError && isConflict(post.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the member; the failed
    // posting is structurally WITHDRAWN — re-entering it is a NEW
    // operator intent whose key rotates via the reload epoch.
    void queryClient.refetchQueries({ queryKey: ["members", "detail", memberId] });
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
    const parsed = feeEntrySchema.safeParse({
      member_id: memberId,
      fee_type: feeType,
      channel,
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
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

  // Structural withdrawal: the ledger accepts no postings for an
  // exited member — the form is not offered (least disclosure).
  const memberExited = freshMember !== undefined && freshMember.status === "exited";

  return (
    <Modal
      title="Post misc fee"
      onClose={onClose}
      closeDisabled={post.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // money posting.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={post.error} onReload={reloadAfterConflict} />
      {post.isError && !conflict && !renderedInline && <ErrorBanner error={post.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            {FEE_TYPE_LABELS[result.fee_type]} posted · ref {result.txn_ref}
          </div>
          <Kv label="Amount (tenant-configured, server-resolved)">
            <span className={styles.netCell}>{fmtKes(result.amount)}</span>
          </Kv>
          <Kv label="Ledger row">
            <span className={styles.mono}>{result.txn_id}</span>
          </Kv>
          <div className={styles.formNote}>
            The amount above is the SERVER&apos;s configured figure from
            the posting response — no amount was (or can be) entered in this
            screen. The row now appears in the transactions register.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                // A NEW intent: entry cleared; the next submission
                // builds fresh key material by content + intent_seq.
                setResult(null);
                setChannel("");
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
          <div className={styles.formNote}>
            The fee AMOUNT is tenant configuration (Settings ▸ Parameters) and
            is resolved by the server — it is deliberately not shown or
            entered here; the posting response reports the resolved figure.
          </div>
          <FormField id="fee-member" label="Member" error={fieldErrors["member_id"]}>
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={memberId}
                onChange={(event) => setMemberId(event.target.value)}
                disabled={post.isPending}
              >
                <option value="">Select a member…</option>
                {memberOptions.map((member) => (
                  <option key={member.id} value={member.id}>
                    {member.name} · {member.member_no}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          {members.hasNextPage && (
            <Button
              type="button"
              onClick={() => void members.fetchNextPage()}
              disabled={members.isFetchingNextPage}
            >
              {members.isFetchingNextPage ? "Loading…" : "Load more members"}
            </Button>
          )}
          {memberId !== "" && memberDetail.isPending && (
            <div className={styles.formNote}>Verifying the member record…</div>
          )}
          {memberId !== "" && memberDetail.isError && <ErrorBanner error={memberDetail.error} />}
          {freshMember !== undefined && !memberExited && (
            <div className={styles.formNote}>
              Member verified: {freshMember.name} · {freshMember.member_no} (record version{" "}
              {freshMember.version}) — read fresh before this posting.
            </div>
          )}
          {memberExited && (
            <Banner variant="error">
              This member has EXITED — the ledger accepts no further postings
              for them (the server enforces this regardless).
            </Banner>
          )}
          <FormField
            id="fee-type"
            label="Fee type"
            error={fieldErrors["fee_type"]}
            hint="Code-owned vocabulary — each type maps to the tenant settings key its amount resolves from."
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={feeType}
                onChange={(event) => setFeeType(event.target.value)}
                disabled={post.isPending}
              >
                {FEE_TYPES.map((option) => (
                  <option key={option} value={option}>
                    {FEE_TYPE_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField id="fee-channel" label="Channel" error={fieldErrors["channel"]}>
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
          <div className={styles.formNote}>
            Money physically moves via M-Pesa or Bank only — accrual and
            internal postings are made by server jobs and are not offered
            here. The posting date is resolved by the server.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={post.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={post.isPending || memberExited}>
              {post.isPending ? "Posting…" : "Post fee…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && freshMember !== undefined && (
        <ConfirmDangerModal
          title="Post misc fee"
          confirmPhrase={freshMember.member_no}
          confirmLabel="Post fee"
          pending={post.isPending}
          onConfirm={() => {
            if (!post.isPending) post.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This posts a {FEE_TYPE_LABELS[confirmEntry.fee_type]} via{" "}
            {CHANNEL_LABELS[confirmEntry.channel]} for {freshMember.name} ·{" "}
            {freshMember.member_no}. The amount is the tenant-configured figure, resolved by the
            server. The ledger is append-only — undoing this requires a
            correction posting.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
