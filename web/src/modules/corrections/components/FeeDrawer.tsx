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
  ErrorMessage,
  announce,
  Select,
} from "@genesis/design-system";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import { MemberLookupField } from "@/modules/members/components/MemberLookupField";
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

  // Member picker: ONE exact-match lookup by the identifier the operator
  // types — never a paged dump of the whole membership. The resolved
  // member only SELECTS; the binding read is the fresh detail read below.
  const [member, setMember] = useState<Member | null>(null);
  const memberId = member?.id ?? "";

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
          
          <MemberLookupField
            idPrefix="fee-member"
            hint="Search by member number or national ID"
            disabled={post.isPending}
            onResolved={setMember}
          />
          {fieldErrors["member_id"] !== undefined && (
            <ErrorMessage id="fee-member-error">{fieldErrors["member_id"]}</ErrorMessage>
          )}
          {memberId !== "" && memberDetail.isPending && (
            <div className={styles.formNote}>Verifying the member record…</div>
          )}
          {memberId !== "" && memberDetail.isError && <ErrorBanner error={memberDetail.error} />}
          {freshMember !== undefined && !memberExited && (
            <div className={styles.formNote}>
              {freshMember.name} · {freshMember.member_no}
            </div>
          )}
          {memberExited && (
            <Banner variant="error">
              This member has EXITED — the ledger accepts no further postings
              for them.
            </Banner>
          )}
          <FormField
            id="fee-type"
            label="Fee type"
            error={fieldErrors["fee_type"]}
          >
            {(control) => (
              <Select
                {...control}
                value={feeType}
                onChange={(event) => setFeeType(event.target.value)}
                disabled={post.isPending}
              >
                {FEE_TYPES.map((option) => (
                  <option key={option} value={option}>
                    {FEE_TYPE_LABELS[option]}
                  </option>
                ))}
              </Select>
            )}
          </FormField>
          <FormField id="fee-channel" label="Channel" error={fieldErrors["channel"]}>
            {(control) => (
              <Select
                {...control}
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
              </Select>
            )}
          </FormField>
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
            {freshMember.member_no}. The amount is the tenant-configured figure
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
