"use client";

/**
 * Request-exit drawer (module 7 — prototype member-exit intake, adapted to the contract): `POST /member-exits` (members:edit)
 * snapshots the settlement (shares + deposits − loan balance −
 * tenant-configured fees) under the member row lock for committee
 * approval.
 *
 * - NO money field exists anywhere in this form (the exit fee comes exclusively from tenant configuration; every settlement figure is computed server-side under locks; `extra="forbid"` server-side) — only the member and an optional reason travel.
 * - FRESH MEMBER READ before arming (record class, staleTime 0;
 *   pattern (e)): the confirmation only arms once the member record
 *   has been read fresh — an already-exited member is withdrawn
 *   structurally (the server rejects regardless).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   member number), pending short-circuit + disabled controls,
 *   `retry: 0`, one Idempotency-Key per logical intent. Key material
 *   folds in the fresh member VERSION, a post-409 reload epoch (F3/W59-2) and a per-success intent counter (W59-3/T2): stable
 *   across pure retries, rotated when the member/reason, the reloaded
 *   record version, an acknowledged conflict OR a recorded success
 *   changes the intent.
 * - A 409 (an open exit already exists for the member, an active loan
 *   is not netted, active guarantees block the exit, or the member
 *   moved underneath) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow: reload refetches the member and the
 *   register with an INFORMATIONAL notice + announce(); the
 *   failed request is NEVER replayed.
 * - The result panel renders the SERVER's snapshot figures VERBATIM
 *   (blocker (a)) — including a possibly NEGATIVE net payable (the
 *   documented loan-exceeds-assets branch). The maker identity this
 *   tab witnessed is recorded for the MakerCheckerPanel's SoD check.
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
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { getOwnUserId } from "@/modules/auth/session";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { fetchMember, fetchMembersPage } from "@/modules/members/api";
import type { Member } from "@/modules/members/schemas";
import { createExitRequest, fetchExitEligibility } from "../api";
import { recordExitMaker } from "../makerRegistry";
import { exitRequestEntrySchema, type ExitRecord, type ExitRequestEntry } from "../schemas";
import styles from "./Exits.module.css";

export function RequestExitDrawer({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [memberId, setMemberId] = useState("");
  const [reason, setReason] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<ExitRequestEntry | null>(null);
  const [result, setResult] = useState<ExitRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload, so a re-entered identical
  // request after a 409 is a NEW intent with a NEW key — never a
  // replay served from the backend idempotency store's pinned outcome.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success intent counter (W59-3/T2): bumped on every SUCCESS, so
  // "Request another" followed by an identical entry is a NEW intent
  // with a NEW key — the server can never silently dedup it.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Member picker: the keyset contract (scalability) — pages of 20 with an
  // explicit "load more". No status filter: exits of arrears or dormant
  // members are legitimate governance flows (Dormant→Exited goes through
  // this workflow); the server enforces eligibility regardless.
  const members = useKeysetList<Member>({
    queryKey: ["members", "list", { status: "", type: "" }],
    fetchPage: (cursor) => fetchMembersPage({ status: "", type: "" }, cursor),
  });
  const memberOptions = members.data?.pages.flatMap((page) => page.items) ?? [];

  // FRESH record read (staleTime 0) the moment a member is chosen —
  // the confirmation only arms once this read has landed (pattern (e)).
  const memberDetail = useQuery({
    queryKey: ["members", "detail", memberId === "" ? "none" : memberId],
    queryFn: () => fetchMember(memberId),
    enabled: memberId !== "",
    staleTime: STALE_TIME.record,
  });
  const freshMember = memberDetail.data;

  // ADVISORY eligibility checklist (the prototype's
  // vExit() criteria rows), read fresh (record class, staleTime 0) the
  // moment a member is chosen and rendered BEFORE submission. Counts
  // and booleans only — the contract carries NO amount. The read is
  // lock-free and ADVISORY: the server re-verdicts every blocker under
  // locks at request time and again at settlement.
  const eligibility = useQuery({
    queryKey: ["exits", "eligibility", memberId === "" ? "none" : memberId],
    queryFn: () => fetchExitEligibility(memberId),
    enabled: memberId !== "",
    staleTime: STALE_TIME.record,
  });
  const facts = eligibility.data;
  // SELF-GATING (least disclosure, the memberExited pattern): when the fresh
  // advisory read reports blockers, the submit affordance is withheld —
  // the server enforces the same blockers under locks regardless. A
  // FAILED advisory read never gates: it is advisory only, and the
  // locked server verdict at request time remains the enforcer.
  const advisoryBlocked = facts !== undefined && !facts.advisory_eligible;

  const create = useMutation({
    // STRUCTURAL freshness guard (the F-M1 class, applied
    // module-wide): the write never fires without the fresh member
    // read (pattern (e)) — its key material folds the REAL member
    // version, never a null sentinel.
    mutationFn: (entry: ExitRequestEntry) => {
      if (freshMember === undefined) {
        return Promise.reject(
          new Error("The fresh member record is not loaded — the exit request was NOT recorded."),
        );
      }
      return createExitRequest(
        entry.member_id,
        entry.reason === "" ? null : entry.reason,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "exit-request",
            entry,
            member_version: freshMember.version,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      );
    },
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form.
      setResult(record);
      // The NEXT request is a new intent even if byte-identical (T2).
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      // SoD witness (per-tab): the signed-in operator initiated this
      // exit — the MakerCheckerPanel withholds their checker controls.
      const ownId = getOwnUserId();
      if (ownId !== null) recordExitMaker(record.id, ownId);
      announce("Exit request recorded — awaiting committee decision.");
      // The register and the member's record are server facts — refetch.
      void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["members", "detail", record.member_id] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirmEntry(null);
      announce("The exit request was NOT recorded.");
    },
  });

  const conflict = create.isError && isConflict(create.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the member (their loans,
    // guarantees or exit slot moved underneath) and the register; the
    // failed request is structurally WITHDRAWN — re-entering it is a
    // NEW operator intent whose key rotates via the reload epoch.
    // NOTHING is replayed.
    void queryClient.refetchQueries({ queryKey: ["members", "detail", memberId] });
    void queryClient.refetchQueries({ queryKey: ["exits", "eligibility", memberId] });
    void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    create.reset();
    setNotice(
      "Record reloaded — the conflicted exit request was withdrawn, nothing was replayed. Re-check the member before re-entering.",
    );
    announce("Record reloaded. The conflicted exit request was withdrawn; nothing was replayed.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending || spent) return;
    const parsed = exitRequestEntrySchema.safeParse({
      member_id: memberId,
      reason: reason.trim(),
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
  const serverErrors = fromApiError(create.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    create.error instanceof ApiError &&
    create.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  // Structural withdrawal: an exited member cannot exit again — the
  // affordance is not offered (least disclosure; the server rejects regardless).
  const memberExited = freshMember !== undefined && freshMember.status === "exited";

  return (
    <Modal
      title="Request member exit"
      onClose={onClose}
      closeDisabled={create.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // exit request — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={create.error} onReload={reloadAfterConflict} />
      {conflict && (
        <div className={styles.formNote}>
          Typical causes: the member already has an open exit request, an
          active loan is not nettable, active guarantees block the exit, or
          the member record moved. Nothing was recorded.
        </div>
      )}
      {create.isError && !conflict && !renderedInline && <ErrorBanner error={create.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Exit requested · awaiting committee decision
          </div>
          <Kv label="Exit record">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Shares (snapshot)">{fmtKes(result.shares_amount)}</Kv>
          <Kv label="Deposits (snapshot)">{fmtKes(result.deposits_amount)}</Kv>
          <Kv label="Loan balance (offset)">{fmtKes(result.loan_balance)}</Kv>
          <Kv label="Exit fees (tenant-configured)">{fmtKes(result.fees)}</Kv>
          <Kv label="Net payable">
            <span className={styles.netCell}>{fmtKes(result.net_payable)}</span>
          </Kv>
          <div className={styles.formNote}>
            Every figure above is the SERVER&apos;s settlement snapshot, computed
            under the member row lock — nothing was computed in this screen.
            The committee approves this exact snapshot; posting re-verifies it
            component-by-component.
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
                // fresh key material by content and intent counter.
                setResult(null);
                setMemberId("");
                setReason("");
                setClientErrors({});
                create.reset();
              }}
            >
              Request another
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <FormField id="exit-member" label="Member" error={fieldErrors["member_id"]}>
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={memberId}
                onChange={(event) => setMemberId(event.target.value)}
                disabled={create.isPending}
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
              Member verified: {freshMember.name} · {freshMember.member_no} (record
              version {freshMember.version}) — read fresh before this request.
            </div>
          )}
          {memberExited && (
            <Banner variant="error">
              This member has already EXITED — no further exit request can be
              recorded for them (the server enforces this regardless).
            </Banner>
          )}
          {memberId !== "" && eligibility.isPending && (
            <div className={styles.formNote}>Loading the advisory eligibility checklist…</div>
          )}
          {memberId !== "" && eligibility.isError && (
            <div className={styles.formNote}>
              The advisory eligibility checklist could not be loaded — it is
              ADVISORY only, so you can still submit: the server enforces every
              blocker under locks at request time regardless.
            </div>
          )}
          {facts !== undefined && (
            // the prototype's vExit() criteria rows — SERVER facts
            // rendered VERBATIM (counts and booleans only; the contract
            // carries NO amount). advisory_eligible is the server's own
            // conjunction — never re-derived from the rows here.
            <div className={styles.resultPanel} data-testid="eligibility-checklist">
              <div className={styles.resultTitle}>Advisory eligibility checklist</div>
              <Kv label="Member status">
                {facts.member_status} —{" "}
                {facts.status_allows_exit ? "allows an exit request" : "BLOCKS an exit request"}
              </Kv>
              <Kv label="Open exit request">
                {facts.open_exit_exists ? "one already exists — BLOCKS" : "none"}
              </Kv>
              <Kv label="Live guarantees (block until released/substituted)">
                {facts.live_guarantees_count}
              </Kv>
              <Kv label="Open loan applications (block until decided)">
                {facts.open_applications_count}
              </Kv>
              <Kv label="Unresolved write-off claim">
                {facts.unresolved_writeoff_claim ? "yes — BLOCKS" : "no"}
              </Kv>
              <Kv label="Active loans (informational — net within the settlement)">
                {facts.active_loans_count}
              </Kv>
              <Kv label="Advisory verdict">
                {facts.advisory_eligible ? "eligible to request" : "blocked"}
              </Kv>
              <div className={styles.formNote}>
                Advisory facts as of {fmtDateTime(facts.as_of)}, read WITHOUT
                locks: the server re-verdicts every blocker under locks when the
                request is recorded and AGAIN at settlement — this checklist
                decides nothing and shows no settlement figure.
              </div>
            </div>
          )}
          {advisoryBlocked && !memberExited && (
            <Banner variant="error">
              Advisory blockers are present — the request affordance is
              withheld here; the server enforces (and re-verdicts) the same
              blockers under locks regardless.
            </Banner>
          )}
          <FormField
            id="exit-reason"
            label="Reason (optional)"
            error={fieldErrors["reason"]}
            hint="Free text for the committee, up to 200 characters. No figure is entered here: the settlement snapshot is computed by the server."
          >
            {(control) => (
              <textarea
                {...control}
                className={styles.textarea}
                maxLength={200}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                disabled={create.isPending}
              />
            )}
          </FormField>
          <div className={styles.formNote}>
            The exit fee comes exclusively from tenant configuration; shares,
            deposits and the loan offset are snapshotted by the server under
            the member row lock. Active guarantees block an exit until
            released or substituted.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={create.isPending}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={create.isPending || memberExited || advisoryBlocked}
            >
              {create.isPending ? "Recording…" : "Request exit…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && freshMember !== undefined && (
        <ConfirmDangerModal
          title="Request member exit"
          confirmPhrase={freshMember.member_no}
          confirmLabel="Request exit"
          pending={create.isPending}
          onConfirm={() => {
            if (!create.isPending) create.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This starts the TERMINAL exit workflow for {freshMember.name} ·{" "}
            {freshMember.member_no}: the server snapshots their full settlement
            (shares + deposits − loan balance − tenant-configured fees) for
            committee approval. You will not be able to vote on or settle your
            own request.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
