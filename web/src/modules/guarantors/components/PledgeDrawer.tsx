"use client";

/**
 * Guarantee-pledge drawer (module 5 — the P9 surface / deferred to this batch): encumbers a guarantor member's savings
 * against another member's application via
 * `POST /applications/{id}/guarantees` (applications:edit).
 *
 * - The drawer fetches the application FRESH (record class, staleTime
 *   0) and offers the pledge ONLY while the stage is pledgeable
 *   (submitted | appraisal | committee) — the UI never offers what the
 *   API forbids (least disclosure). The server re-checks the stage AND the
 *   guarantor's capacity under the application row lock + the
 *   guarantor's deposit-account row lock; an over-pledge from a stale
 *   read is therefore impossible server-side, and this drawer NEVER
 *   works from a cached record.
 * - SELF-GUARANTEE is structurally impossible from this client: the
 *   borrower never appears among the guarantor options (and the server
 *   rejects a self-pledge regardless — 400).
 * - Guarantor free capacity shown here is the SERVER's aggregate
 *   figure, re-fetched fresh on drawer mount (staleTime 0) — nothing is
 *   computed client-side (blocker (a)); a member without live pledges
 *   is not in the slice and the capacity is stated as server-verified
 *   at pledge time instead.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit + disabled controls, `retry: 0`, one
 *   Idempotency-Key per logical intent (stable across retries, rotated
 *   when the guarantor or the amount changes). After a recorded pledge
 *   the affordance is SPENT — the result panel replaces the form.
 * - A 409 (stage moved underneath, or capacity exhausted by a
 *   concurrent pledge/withdrawal) renders the shared ConflictBanner's
 *   explicit reload-and-re-enter flow — reload refetches the record and
 *   the server aggregates and NEVER replays the write.
 * - The amount is a decimal STRING end-to-end; the created record
 *   renders VERBATIM from the response through the Zod boundary.
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
import { fetchApplication } from "@/modules/applications/api";
import type { Product } from "@/modules/products/schemas";
import { fetchDashboardSummary } from "@/modules/dashboard/api";
import { pledgeGuarantee } from "../api";
import { recordWitnessedGuarantee } from "../sessionRegistry";
import {
  PLEDGEABLE_STAGES,
  pledgeCreateSchema,
  type Guarantee,
  type PledgeCreateInput,
} from "../schemas";
import { guaranteeStatusPill } from "./pills";
import styles from "./Guarantors.module.css";

export function PledgeDrawer({
  applicationId,
  products,
  onClose,
}: Readonly<{
  applicationId: string;
  products: Product[];
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [guarantorId, setGuarantorId] = useState("");
  const [amount, setAmount] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<Guarantee | null>(null);
  const [notice, setNotice] = useState<string>("");
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Record class (staleTime 0): NEVER a stale application record into a
  // money-adjacent write — the stage below is the freshest available read.
  const detail = useQuery({
    queryKey: ["applications", "detail", applicationId],
    queryFn: () => fetchApplication(applicationId),
    staleTime: STALE_TIME.record,
  });

  const borrowerId = detail.data?.member_id;
  const borrower = useQuery({
    queryKey: ["members", "detail", borrowerId ?? "pending"],
    queryFn: () => {
      if (borrowerId === undefined) return Promise.reject(new Error("member id not loaded"));
      return fetchMember(borrowerId);
    },
    enabled: borrowerId !== undefined,
    staleTime: STALE_TIME.record,
  });

  // The SERVER's guarantor aggregates, re-fetched FRESH on every
  // drawer mount (record class): the free-capacity figure shown next to
  // the picker is never served from a stale cache before this write.
  const aggregates = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: fetchDashboardSummary,
    staleTime: STALE_TIME.record,
    refetchOnMount: "always",
  });

  // Guarantor picker: ACTIVE members via the keyset contract (scalability)
  // — pages of 20 with an explicit "load more". The P8 list has no
  // search parameter; recorded honestly as a UX limitation in the MR.
  const members = useKeysetList<Member>({
    queryKey: ["members", "list", { status: "active", type: "" }],
    fetchPage: (cursor) => fetchMembersPage({ status: "active", type: "" }, cursor),
  });
  // SELF-GUARANTEE prevention (structural): the borrower is never an
  // option — there is no way to select them from this client.
  const guarantorOptions = (members.data?.pages.flatMap((page) => page.items) ?? []).filter(
    (member) => member.id !== borrowerId,
  );

  const pledge = useMutation({
    mutationFn: (input: PledgeCreateInput) =>
      pledgeGuarantee(
        applicationId,
        input,
        idempotencyKeyFor(
          keySlot.current,
          // Key material = intent + FRESHNESS: the version of
          // the fresh (staleTime 0) application read anchors the key to
          // the record state the operator acted on. Pure retries of one
          // intent keep the key STABLE; a changed guarantor/amount OR a
          // post-409 reload that bumped the record version ROTATES it —
          // a legitimate re-submission after a conflict can never be
          // served a stale pinned outcome by the backend key store.
          JSON.stringify({
            op: "pledge-guarantee",
            id: applicationId,
            recordVersion: detail.data?.version ?? null,
            input,
          }),
        ),
      ),
    onSuccess: (created) => {
      setConfirming(false);
      // SPENT affordance: the result panel replaces the form; the
      // witnessed registry now carries the record for consent/release.
      setResult(created);
      recordWitnessedGuarantee(created);
      announce("Guarantee pledged.");
      void queryClient.invalidateQueries({ queryKey: ["dashboard", "summary"] });
      void queryClient.invalidateQueries({ queryKey: ["applications", "detail", applicationId] });
      void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirming(false);
      announce("Guarantee was not pledged.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Pledge guarantee" onClose={onClose}>
        <div className={styles.muted}>Loading…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Pledge guarantee" onClose={onClose}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const app = detail.data;
  const product = products.find((p) => p.id === app.product_id);
  const conflict = pledge.isError && isConflict(pledge.error);
  const pledgeable =
    (PLEDGEABLE_STAGES as readonly string[]).includes(app.stage) && result === null;

  // The SERVER's free-capacity figure for the chosen guarantor, when the
  //  slice carries them (it lists members WITH live pledges, by
  // exposure). Absence is stated honestly — never computed locally.
  const guarantorsSlice = aggregates.data?.guarantors ?? null;
  const chosenCapacity =
    guarantorsSlice?.guarantors.find((row) => row.member_id === guarantorId) ?? null;

  function reloadRecord() {
    // Explicit reload flow: refetch the application (its stage may have
    // moved) and the server aggregates (capacity may be exhausted); the
    // stale write is NEVER replayed — re-entering different details is a
    // NEW intent with a rotated Idempotency-Key.
    void queryClient.refetchQueries({ queryKey: ["applications", "detail", applicationId] });
    void queryClient.refetchQueries({ queryKey: ["dashboard", "summary"] });
    pledge.reset();
    setNotice("Record reloaded — re-check the stage and capacity before acting again.");
    // Every async outcome is announced: the post-conflict
    // reload is an outcome, not a success.
    announce("Record reloaded after the conflict — re-check the stage and capacity.");
  }

  function armConfirmation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pledge.isPending) return;
    const parsed = pledgeCreateSchema.safeParse({
      guarantor_member_id: guarantorId,
      amount: amount.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    setConfirming(true);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(pledge.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    pledge.error instanceof ApiError &&
    pledge.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  const chosenGuarantor = guarantorOptions.find((member) => member.id === guarantorId);

  return (
    <Modal
      title="Pledge guarantee"
      onClose={onClose}
      closeDisabled={pledge.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // pledge form — dismissal is the explicit ✕ or Escape. (This drawer
      // arrived via the main-merge after the W56-3 sweep; convention
      // completed here.)
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling: the notice reports a
          post-conflict reload. */}
      {notice !== "" && <Banner variant="info">{notice}</Banner>}
      <div className={styles.detailGrid}>
        <Kv label="Borrower">
          {borrower.data !== undefined ? (
            <>
              {borrower.data.name} · {borrower.data.member_no}
            </>
          ) : (
            <span className={styles.mono}>{app.member_id}</span>
          )}
        </Kv>
        <Kv label="Product">
          {product !== undefined ? product.name : <span className={styles.mono}>{app.product_id}</span>}
        </Kv>
        <Kv label="Applied amount">{fmtKes(app.amount)}</Kv>
        <Kv label="Security cover">{app.cover_pct}% (server-computed)</Kv>
        <Kv label="Stage">{app.stage}</Kv>
      </div>

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={pledge.error} onReload={reloadRecord} />
      {pledge.isError && !conflict && !renderedInline && <ErrorBanner error={pledge.error} />}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Guarantee pledged</div>
          <Kv label="Guarantee record">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Pledged amount">{fmtKes(result.amount)}</Kv>
          <Kv label="Status">{guaranteeStatusPill(result.status)}</Kv>
          <div className={styles.formNote}>
            Every figure above came from the server&apos;s response. The pledge now
            appears under &quot;This session&apos;s guarantees&quot; for consent and release;
            the guarantor&apos;s consent must be recorded before the pledge counts as
            active collateral.
          </div>
        </div>
      )}

      {pledgeable && (
        <form onSubmit={armConfirmation} noValidate>
          <FormField
            id="pledge-guarantor"
            label="Guarantor"
            error={fieldErrors["guarantor_member_id"]}
            hint="Active members only; the borrower cannot guarantee their own loan and is not offered."
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={guarantorId}
                onChange={(event) => setGuarantorId(event.target.value)}
                disabled={pledge.isPending}
              >
                <option value="">Select a guarantor…</option>
                {guarantorOptions.map((member) => (
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
          {guarantorId !== "" && (
            <div className={styles.formNote}>
              {chosenCapacity !== null ? (
                <>
                  Server-reported free capacity for this guarantor:{" "}
                  <strong>{fmtKes(chosenCapacity.free_capacity)}</strong> (live pledges{" "}
                  {fmtKes(chosenCapacity.pledged_total)}).
                </>
              ) : (
                <>
                  No live pledges reported for this member by the server aggregate; their
                  capacity is verified by the server under lock at pledge time.
                </>
              )}
            </div>
          )}
          <FormField
            id="pledge-amount"
            label="Pledge amount (KES)"
            error={fieldErrors["amount"]}
            hint="Decimal amount, e.g. 50000 or 50000.00 — capacity is checked by the server under the guarantor's account lock."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={18}
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                disabled={pledge.isPending}
              />
            )}
          </FormField>
          <div className={styles.formNote}>
            Nothing monetary is calculated in this form: the capacity check, the cover
            recomputation and every figure shown are server facts.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={pledge.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={pledge.isPending}>
              {pledge.isPending ? "Pledging…" : "Pledge…"}
            </Button>
          </div>
        </form>
      )}
      {!pledgeable && result === null && (
        <div className={styles.formNote}>
          This application is no longer in a pledgeable stage (submitted, appraisal or
          committee) — pledging is not offered (the server enforces the stage regardless).
        </div>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Encumber savings"
          confirmPhrase={app.id.slice(0, 8)}
          confirmLabel="Pledge guarantee"
          pending={pledge.isPending}
          onConfirm={() => {
            if (pledge.isPending) return;
            const parsed = pledgeCreateSchema.safeParse({
              guarantor_member_id: guarantorId,
              amount: amount.trim(),
            });
            if (!parsed.success) return;
            pledge.mutate(parsed.data);
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This encumbers {fmtKes(amount.trim())} of{" "}
            {chosenGuarantor !== undefined ? chosenGuarantor.name : "the selected guarantor"}
            &apos;s savings against{" "}
            {borrower.data !== undefined ? borrower.data.name : "the borrower"}&apos;s
            application until released. The capacity check runs on the server under the
            guarantor&apos;s account lock.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
