"use client";

/**
 * Guarantee-substitution drawer (module 5 — atomic swap):
 * replaces a disbursed loan's collateral via
 * `POST /guarantees/{id}/substitute` (applications:edit).
 *
 * - Release of the old guarantee and creation of the replacement
 *   CONSENTED pledge happen in ONE server transaction; any failure
 *   leaves the original fully intact — nothing is orchestrated
 *   client-side.
 * - The substitute's consent is a first-class audited attestation
 *   the form requires a consent reference citing its evidence
 *   (an unreferenced substitute is a server 422; the server verdict
 *   wins per field). The pre- caller-asserted `consented` boolean
 *   is REMOVED from the contract — the attestation is the staff act
 *   plus its cited evidence, never a checkbox (the lesson).
 * - The replacement amount is OPTIONAL: blank lets the server derive it
 *   from the guarantee row (it may only meet or exceed the released
 *   amount — enforced server-side, never computed here).
 * - SELF-GUARANTEE stays structurally impossible: the borrower is never
 *   among the substitute options (the server rejects it regardless).
 * - EXACTLY ONE versioned write per intent (typed phrase, pending
 *   short-circuit, retry: 0, stable/rotating Idempotency-Key). A 409
 *   renders the shared ConflictBanner; with NO per-guarantee read in
 *   the contract, reload refetches the server aggregates and
 *   structurally WITHDRAWS this tab's stale copy — never a replay.
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
import { substituteGuarantee } from "../api";
import { markWitnessedConflict, recordWitnessedGuarantee } from "../sessionRegistry";
import {
  substituteCreateSchema,
  type Guarantee,
  type SubstituteCreateInput,
  type Substitution,
} from "../schemas";
import { guaranteeStatusPill } from "./pills";
import styles from "./Guarantors.module.css";

export function SubstituteDrawer({
  guarantee,
  onClose,
}: Readonly<{
  guarantee: Guarantee;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [guarantorId, setGuarantorId] = useState("");
  const [consentReference, setConsentReference] = useState("");
  const [amount, setAmount] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<Substitution | null>(null);
  const [withdrawn, setWithdrawn] = useState(false);
  const [notice, setNotice] = useState<string>("");
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const borrower = useQuery({
    queryKey: ["members", "detail", guarantee.borrower_member_id],
    queryFn: () => fetchMember(guarantee.borrower_member_id),
    staleTime: STALE_TIME.record,
  });

  // Substitute picker: ACTIVE members via the keyset contract (scalability).
  const members = useKeysetList<Member>({
    queryKey: ["members", "list", { status: "active", type: "" }],
    fetchPage: (cursor) => fetchMembersPage({ status: "active", type: "" }, cursor),
  });
  // SELF-GUARANTEE prevention (structural): the borrower is never an
  // option for their own loan's replacement collateral.
  const substituteOptions = (members.data?.pages.flatMap((page) => page.items) ?? []).filter(
    (member) => member.id !== guarantee.borrower_member_id,
  );

  const substitute = useMutation({
    mutationFn: (input: SubstituteCreateInput) =>
      substituteGuarantee(
        guarantee.id,
        guarantee.version,
        input,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "substitute-guarantee",
            id: guarantee.id,
            version: guarantee.version,
            input,
          }),
        ),
      ),
    onSuccess: (swap) => {
      setConfirming(false);
      // SPENT affordance: the result panel replaces the form; BOTH sides
      // of the swap enter the witnessed registry verbatim.
      setResult(swap);
      recordWitnessedGuarantee(swap.released);
      recordWitnessedGuarantee(swap.replacement);
      announce("Guarantee substituted.");
      void queryClient.invalidateQueries({ queryKey: ["dashboard", "summary"] });
    },
    onError: () => {
      setConfirming(false);
      announce("The guarantee was not substituted.");
    },
  });

  const conflict = substitute.isError && isConflict(substitute.error);
  const actionable = result === null && !withdrawn;

  function reloadAfterConflict() {
    // Explicit reload flow — no per-guarantee read exists: refetch the
    // aggregates, withdraw this tab's stale copy, never replay.
    markWitnessedConflict(guarantee.id);
    setWithdrawn(true);
    void queryClient.refetchQueries({ queryKey: ["dashboard", "summary"] });
    substitute.reset();
    setNotice(
      "This guarantee changed outside this tab and its actions are withdrawn here — the contract exposes no per-guarantee read to refresh it.",
    );
    // Every async outcome is announced: the withdrawal is an
    // outcome, not a success — the live region must say so.
    announce("Guarantee actions withdrawn — the record changed outside this tab.");
  }

  function parsedInput() {
    return substituteCreateSchema.safeParse({
      guarantor_member_id: guarantorId,
      consent_reference: consentReference,
      amount: amount.trim() === "" ? null : amount.trim(),
    });
  }

  function armConfirmation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (substitute.isPending) return;
    const parsed = parsedInput();
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    setConfirming(true);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(substitute.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    substitute.error instanceof ApiError &&
    substitute.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  const chosenSubstitute = substituteOptions.find((member) => member.id === guarantorId);

  return (
    <Modal
      title="Substitute guarantee"
      onClose={onClose}
      closeDisabled={substitute.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // substitution form (attestation + evidence reference) — dismissal
      // is the explicit ✕ or Escape. (This drawer arrived via the
      // main-merge after the W56-3 sweep; convention completed here.)
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling: the notice reports a
          CONFLICTED/WITHDRAWN record. */}
      {notice !== "" && <Banner variant="info">{notice}</Banner>}
      <div className={styles.detailGrid}>
        <Kv label="Guarantee record">
          <span className={styles.mono}>{guarantee.id}</span>
        </Kv>
        <Kv label="Borrower">
          {borrower.data !== undefined ? (
            <>
              {borrower.data.name} · {borrower.data.member_no}
            </>
          ) : (
            <span className={styles.mono}>{guarantee.borrower_member_id}</span>
          )}
        </Kv>
        <Kv label="Pledged amount">{fmtKes(guarantee.amount)}</Kv>
        <Kv label="Status">{guaranteeStatusPill(guarantee.status)}</Kv>
        <Kv label="Record version">{guarantee.version}</Kv>
      </div>

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={substitute.error} onReload={reloadAfterConflict} />
      {substitute.isError && !conflict && !renderedInline && (
        <ErrorBanner error={substitute.error} />
      )}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Substitution recorded (atomic swap)</div>
          <Kv label="Released record">
            <span className={styles.mono}>{result.released.id}</span>
          </Kv>
          <Kv label="Released status">{guaranteeStatusPill(result.released.status)}</Kv>
          <Kv label="Replacement record">
            <span className={styles.mono}>{result.replacement.id}</span>
          </Kv>
          <Kv label="Replacement amount">{fmtKes(result.replacement.amount)}</Kv>
          <Kv label="Replacement status">{guaranteeStatusPill(result.replacement.status)}</Kv>
          <div className={styles.formNote}>
            Both records above are the server&apos;s word from one atomic transaction —
            nothing was computed or sequenced in this screen.
          </div>
        </div>
      )}

      {actionable && (
        <form onSubmit={armConfirmation} noValidate>
          <FormField
            id="substitute-guarantor"
            label="Substitute guarantor"
            error={fieldErrors["guarantor_member_id"]}
            hint="Active members only; the borrower cannot guarantee their own loan and is not offered."
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={guarantorId}
                onChange={(event) => setGuarantorId(event.target.value)}
                disabled={substitute.isPending}
              >
                <option value="">Select a member…</option>
                {substituteOptions.map((member) => (
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
          <FormField
            id="substitute-consent-reference"
            label="Consent evidence reference"
            error={fieldErrors["consent_reference"]}
            hint="Cite the evidence the substitute guarantor's consent rests on (e.g. the signed guarantorship form) — submitting IS the staff attestation and it is written as an audited fact."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={200}
                value={consentReference}
                onChange={(event) => setConsentReference(event.target.value)}
                disabled={substitute.isPending}
              />
            )}
          </FormField>
          <FormField
            id="substitute-amount"
            label="Replacement amount (KES) — optional"
            error={fieldErrors["amount"]}
            hint="Leave blank to match the released amount (the server derives it); a typed amount may only meet or exceed it — enforced server-side."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={18}
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                disabled={substitute.isPending}
              />
            )}
          </FormField>
          <div className={styles.formNote}>
            Nothing monetary is calculated in this form: the released amount, the
            replacement floor and the substitute&apos;s capacity are all verified by the
            server inside one transaction.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={substitute.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={substitute.isPending}>
              {substitute.isPending ? "Substituting…" : "Substitute…"}
            </Button>
          </div>
        </form>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Confirm substitution"
          confirmPhrase={guarantee.id.slice(0, 8)}
          confirmLabel="Substitute guarantee"
          pending={substitute.isPending}
          onConfirm={() => {
            if (substitute.isPending) return;
            const parsed = parsedInput();
            if (!parsed.success) return;
            substitute.mutate(parsed.data);
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This releases the current {fmtKes(guarantee.amount)} pledge and encumbers{" "}
            {chosenSubstitute !== undefined ? chosenSubstitute.name : "the substitute"}
            &apos;s savings in its place, in ONE atomic server transaction. A failure
            leaves the original guarantee fully intact.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
