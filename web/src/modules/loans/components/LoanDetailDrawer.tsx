"use client";

/**
 * Loan detail drawer (module 4 — prototype `vBook` drawer): terms,
 * balances, repayment schedule, early settlement quote and repayment
 * recording.
 *
 * - EVERY figure renders VERBATIM from API decimal strings (blocker
 *   (a)): outstanding balance, penalty due, schedule installments, the
 *   settlement quote and the repayment allocation are SERVER facts.
 *   The prototype's locally derived figures (provision KES = balance
 *   times pct, remaining installment) are deliberately NOT reproduced —
 *   no API endpoint computes them per loan; the provisioning TOTAL
 *   lives on the server portfolio summary.
 * - Repayment recording is a MONEY WRITE: typed confirmation
 *   (ConfirmDangerModal), one Idempotency-Key per logical intent
 *   (stable across retries, rotates when the entry changes), retry: 0,
 *   pending short-circuit. A 409 (loan closed / written off underneath)
 *   renders the shared ConflictBanner's explicit reload-and-re-enter
 *   flow — never a silent overwrite, never an auto-retry.
 * - The repayment affordance follows the P4 matrix: the API guards
 *   POST /loans/{id}/repayments with transactions:create, so the form
 *   mounts only with that grant AND only on an ACTIVE loan (the UI never offers what the API forbids — least disclosure).
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
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import type { Product } from "@/modules/products/schemas";
import {
  fetchLoan,
  fetchLoanSchedule,
  fetchSettlementQuote,
  recordRepayment,
} from "../api";
import {
  CASH_CHANNELS,
  CHANNEL_LABELS,
  repaymentCreateSchema,
  type RepaymentCreateInput,
  type RepaymentResult,
} from "../schemas";
import { loanClassPill, loanStatusPill } from "./pills";
import { ScheduleTable } from "./ScheduleTable";
import styles from "./Loans.module.css";

export function LoanDetailDrawer({
  loanId,
  products,
  onClose,
}: Readonly<{
  loanId: string;
  products: Product[];
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [amount, setAmount] = useState("");
  const [channel, setChannel] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmRepay, setConfirmRepay] = useState<RepaymentCreateInput | null>(null);
  const [result, setResult] = useState<RepaymentResult | null>(null);
  const [quoteRequested, setQuoteRequested] = useState(false);
  const [notice, setNotice] = useState<string>("");
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  // Per-posting intent counter (W59-3, the T2 class): bumped on every
  // SUCCESSFUL posting so a teller re-entering an IDENTICAL repayment
  // (routine — e.g. two equal instalment payments the same day) is a NEW
  // posting intent with a NEW key. Stability across retries of one
  // UNPOSTED intent is untouched (the counter only moves on success).
  const postingSeq = useRef(0);

  const detail = useQuery({
    queryKey: ["loans", "detail", loanId],
    queryFn: () => fetchLoan(loanId),
    // Record class: NEVER a stale record into a money write.
    staleTime: STALE_TIME.record,
  });

  const memberId = detail.data?.member_id;
  const member = useQuery({
    queryKey: ["members", "detail", memberId ?? "pending"],
    queryFn: () => {
      if (memberId === undefined) return Promise.reject(new Error("member id not loaded"));
      return fetchMember(memberId);
    },
    enabled: memberId !== undefined,
    staleTime: STALE_TIME.record,
  });

  const schedule = useQuery({
    queryKey: ["loans", "schedule", loanId],
    queryFn: () => fetchLoanSchedule(loanId),
    staleTime: STALE_TIME.record,
  });

  const quote = useQuery({
    queryKey: ["loans", "settlement", loanId],
    queryFn: () => fetchSettlementQuote(loanId),
    enabled: quoteRequested,
    staleTime: STALE_TIME.record,
  });

  const repay = useMutation({
    mutationFn: (input: RepaymentCreateInput) =>
      recordRepayment(
        loanId,
        input,
        idempotencyKeyFor(
          keySlot.current,
          // Key material = intent + FRESHNESS (W59-2, the F3 class):
          // the version of the fresh (staleTime 0) loan read anchors the
          // key to the record state the operator acted on — stable
          // across pure retries, rotated when the entry changes OR when
          // a post-409 reload bumped the loan version.
          JSON.stringify({
            op: "repayment",
            id: loanId,
            recordVersion: detail.data?.version ?? null,
            // W59-3: identical legitimate re-postings are distinct
            // intents — the server must never dedup them silently.
            postingSeq: postingSeq.current,
            input,
          }),
        ),
      ),
    onSuccess: (recorded) => {
      setConfirmRepay(null);
      setResult(recorded);
      // The posting landed: the NEXT posting — even a byte-identical
      // re-entry — is a NEW intent and must carry a NEW key (W59-3);
      // otherwise the server dedups it and the ledger silently omits a
      // legitimate second payment while the screen shows success.
      postingSeq.current += 1;
      setAmount("");
      setChannel("");
      setClientErrors({});
      setNotice("");
      announce("Repayment recorded.");
      // The balance, status, schedule and aggregates are server facts
      // — refetch them.
      void queryClient.invalidateQueries({ queryKey: ["loans", "detail", loanId] });
      void queryClient.invalidateQueries({ queryKey: ["loans", "schedule", loanId] });
      void queryClient.invalidateQueries({ queryKey: ["loans", "settlement", loanId] });
      void queryClient.invalidateQueries({ queryKey: ["loans", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["portfolio", "summary"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirmRepay(null);
      announce("Repayment was not recorded.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Loan detail" onClose={onClose}>
        <div className={styles.muted}>Loading…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Loan detail" onClose={onClose}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const loan = detail.data;
  const mayRepay = can(permissions.data, "transactions", "create");
  const product = products.find((p) => p.id === loan.product_id);
  const conflict = repay.isError && isConflict(repay.error);

  function reloadRecord() {
    // Explicit reload flow: refetch the loan and its schedule; the failed
    // repayment is NEVER replayed (recording again is a NEW operator
    // intent re-entered against the fresh record).
    void queryClient.refetchQueries({ queryKey: ["loans", "detail", loanId] });
    void queryClient.refetchQueries({ queryKey: ["loans", "schedule", loanId] });
    repay.reset();
    setNotice("Record reloaded — re-check the loan before acting again.");
    // Every async outcome is announced: the post-conflict
    // reload is an outcome, not a success (W59-4, the F5 class).
    announce("Record reloaded after the conflict — re-check the loan.");
  }

  function submitRepayment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (repay.isPending) return;
    const parsed = repaymentCreateSchema.safeParse({
      amount: amount.trim(),
      channel,
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    // Arm the typed confirmation — the write happens only after the
    // operator types the byte-identical phrase.
    setConfirmRepay(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(repay.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    repay.error instanceof ApiError &&
    repay.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    // W56-3: the drawer hosts the repayment entry form — a stray overlay
    // click must never discard operator input; dismissal is ✕ or Escape.
    <Modal
      title="Loan detail"
      onClose={onClose}
      closeDisabled={repay.isPending}
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling: the notice reports a
          post-conflict reload (W59-4, the F5 class). */}
      {notice !== "" && <Banner variant="info">{notice}</Banner>}
      <div className={styles.detailGrid}>
        <Kv label="Member">
          {member.data !== undefined ? (
            <>
              {member.data.name} · {member.data.member_no}
            </>
          ) : (
            <span className={styles.mono}>{loan.member_id}</span>
          )}
        </Kv>
        <Kv label="Product">
          {product !== undefined ? product.name : <span className={styles.mono}>{loan.product_id}</span>}
        </Kv>
        <Kv label="Principal">{fmtKes(loan.principal)}</Kv>
        <Kv label="Outstanding balance">{fmtKes(loan.balance)}</Kv>
        <Kv label="Penalty due">{fmtKes(loan.penalty_due)}</Kv>
        <Kv label="Rate">{loan.rate_pct}% (product-set)</Kv>
        <Kv label="Term">{loan.term_months} months</Kv>
        <Kv label="Status">{loanStatusPill(loan.status)}</Kv>
        <Kv label="Classification">{loanClassPill(loan.classification)}</Kv>
        <Kv label="Days past due">{loan.days_past_due} days</Kv>
        <Kv label="Provision rate">{loan.provision_pct}% (per tenant policy)</Kv>
        <Kv label="Disbursed">{fmtDateTime(loan.disbursed_at)}</Kv>
        <Kv label="Closed">{fmtDateTime(loan.closed_at)}</Kv>
        <Kv label="Record version">{loan.version}</Kv>
      </div>

      <div className={styles.subhead}>Repayment schedule</div>
      {schedule.isPending && <div className={styles.muted}>Loading schedule…</div>}
      {schedule.isError && <ErrorBanner error={schedule.error} />}
      {schedule.data !== undefined && <ScheduleTable rows={schedule.data} />}

      <div className={styles.subhead}>Early settlement</div>
      {!quoteRequested && (
        <Button type="button" onClick={() => setQuoteRequested(true)}>
          Get settlement quote
        </Button>
      )}
      {quoteRequested && quote.isPending && (
        <div className={styles.muted}>Fetching quote…</div>
      )}
      {quoteRequested && quote.isError && <ErrorBanner error={quote.error} />}
      {quote.data !== undefined && (
        <div className={styles.resultPanel}>
          <div className={styles.resultTitle}>Settlement quote · as of {quote.data.as_of}</div>
          <Kv label="Principal balance">{fmtKes(quote.data.principal_balance)}</Kv>
          <Kv label="Interest due">{fmtKes(quote.data.interest_due)}</Kv>
          <Kv label="Penalties due">{fmtKes(quote.data.penalties_due)}</Kv>
          <Kv label="Total to settle">{fmtKes(quote.data.total)}</Kv>
          <div className={styles.formNote}>
            Every settlement figure is computed by the server as of the stated
            date — nothing is derived in this screen.
          </div>
        </div>
      )}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={repay.error} onReload={reloadRecord} />
      {repay.isError && !conflict && !renderedInline && <ErrorBanner error={repay.error} />}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Repayment recorded · ref {result.txn_ref}</div>
          <Kv label="Allocated to penalties">{fmtKes(result.penalties)}</Kv>
          <Kv label="Allocated to interest">{fmtKes(result.interest)}</Kv>
          <Kv label="Allocated to principal">{fmtKes(result.principal)}</Kv>
          <Kv label="Balance after">{fmtKes(result.balance_after)}</Kv>
          <Kv label="Penalty due after">{fmtKes(result.penalty_due_after)}</Kv>
          <Kv label="Loan status">{loanStatusPill(result.status)}</Kv>
        </div>
      )}

      {mayRepay && loan.status === "active" && (
        <>
          <div className={styles.subhead}>Record repayment</div>
          <form onSubmit={submitRepayment} noValidate>
            <FormField
              id="repay-amount"
              label="Amount (KES)"
              error={fieldErrors["amount"]}
              hint="Decimal amount, e.g. 5000 or 5000.50 — the server allocates it to penalties, then interest, then principal."
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={18}
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                />
              )}
            </FormField>
            <FormField id="repay-channel" label="Channel" error={fieldErrors["channel"]}>
              {(control) => (
                <select
                  {...control}
                  className={styles.select}
                  value={channel}
                  onChange={(event) => setChannel(event.target.value)}
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
            <div className={styles.actions}>
              <Button type="submit" variant="primary" disabled={repay.isPending}>
                {repay.isPending ? "Recording…" : "Record repayment…"}
              </Button>
            </div>
          </form>
        </>
      )}
      {mayRepay && loan.status !== "active" && (
        <div className={styles.formNote}>
          This loan is not active — the contract accepts no further repayments
          through this screen.
        </div>
      )}

      {confirmRepay !== null && (
        <ConfirmDangerModal
          title="Record repayment"
          confirmPhrase={loan.id.slice(0, 8)}
          confirmLabel="Record repayment"
          pending={repay.isPending}
          onConfirm={() => {
            if (!repay.isPending) repay.mutate(confirmRepay);
          }}
          onClose={() => setConfirmRepay(null)}
        >
          <Banner>
            This posts {fmtKes(confirmRepay.amount)} via{" "}
            {CHANNEL_LABELS[confirmRepay.channel]} to the ledger. The server
            allocates it to penalties, then interest, then principal; the
            posting cannot be undone from this screen.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
