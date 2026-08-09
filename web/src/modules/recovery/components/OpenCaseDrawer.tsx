"use client";

/**
 * Open-recovery-case drawer (the prototype's "Initiate recovery" action, adapted to the contract):
 * `POST /recovery-cases` (loan_book:create) opens a case on a
 * non-performing ACTIVE loan.
 *
 * - The body carries the loan id ONLY (extra="forbid"): the NPL
 *   snapshot (classification, days past due) is read under the loan
 *   row lock and every SLA timestamp is written server-side (addendum
 *   A7) — no classification, DPD or date field can even be sent.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   loan id prefix), pending short-circuit, `retry: 0`, one
 *   Idempotency-Key per logical intent — simple-create material
 *   (README rule 2; the server holds the one-LIVE-case-per-loan claim,
 *   0033 uq_recovery_cases_one_open).
 * - A 409 (a live case already exists, or the loan is not an active
 *   non-performing one) renders the ConflictBanner's explicit
 *   reload-and-re-enter
 *   flow — NOTHING is replayed.
 * - The result panel renders the SERVER's case record verbatim; the
 *   affordance is then SPENT. NO money figure exists anywhere on this
 *   surface (least disclosure, addendum).
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
import { fmtDateTime } from "@/lib/format";
import { loanClassPill } from "@/modules/loans/components/pills";
import { openCase } from "../api";
import { openCaseEntrySchema, type CaseRecord, type OpenCaseEntry } from "../schemas";
import { caseStatusPill } from "./pills";
import styles from "./Recovery.module.css";

export function OpenCaseDrawer({
  onOpened,
  onClose,
}: Readonly<{
  /** Open the created case in the detail drawer. */
  onOpened: (caseId: string) => void;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [loanId, setLoanId] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<OpenCaseEntry | null>(null);
  const [result, setResult] = useState<CaseRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const open = useMutation({
    mutationFn: (entry: OpenCaseEntry) =>
      openCase(
        entry.loan_id,
        idempotencyKeyFor(
          keySlot.current,
          // Simple-create material (README rule 2): op + the full
          // canonical body; the 0033 one-LIVE-case partial-unique
          // index is the uniqueness authority.
          JSON.stringify({
            op: "recovery-case-open",
            loan_id: entry.loan_id,
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form.
      setResult(record);
      setNotice("");
      announce("Recovery case opened — it now appears on the worklist.");
      void queryClient.invalidateQueries({ queryKey: ["recovery", "worklist"] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The recovery case was NOT opened.");
    },
  });

  const conflict = open.isError && isConflict(open.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: a live case already claims this
    // loan (one live case per loan), or the loan is not a
    // non-performing active loan. NOTHING is replayed.
    void queryClient.invalidateQueries({ queryKey: ["recovery", "worklist"] });
    setReloadEpoch((epoch) => epoch + 1);
    open.reset();
    setNotice(
      "Nothing was opened — this loan may already carry a live case (one live case per loan; find it on the worklist), or it is not an NPL-classified active loan.",
    );
    announce("Nothing was opened — re-check the loan before re-entering.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (open.isPending || spent) return;
    const parsed = openCaseEntrySchema.safeParse({ loan_id: loanId.trim() });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    setConfirmEntry(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(open.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    open.error instanceof ApiError &&
    open.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <Modal
      title="Initiate recovery"
      onClose={onClose}
      closeDisabled={open.isPending}
      // W56-3: a stray overlay click must never discard the entry.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={open.error} onReload={reloadAfterConflict} />
      {open.isError && !conflict && !renderedInline && <ErrorBanner error={open.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Recovery case opened</div>
          <Kv label="Status">{caseStatusPill(result.status)}</Kv>
          <Kv label="Case id">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Classification at open">{loanClassPill(result.classification_at_open)}</Kv>
          <Kv label="Days past due at open">
            <span className={styles.dpdCell}>{result.days_past_due_at_open}</span>
          </Kv>
          <Kv label="Opened">{fmtDateTime(result.opened_at)}</Kv>
          <div className={styles.formNote}>
            The NPL snapshot above is the SERVER&apos;s, read under the loan
            row lock — nothing was computed or dated in this screen. The case
            is unassigned: assign an officer from the case drawer.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button type="button" variant="primary" onClick={() => onOpened(result.id)}>
              Open case
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <div className={styles.formNote}>
            Recovery opens on a non-performing ACTIVE loan (&gt;90 days past
            due — the server verifies under the loan row lock). One live case
            per loan; cure and write-off close cases automatically from loan
            facts — never by hand.
          </div>
          <FormField
            id="case-loan-id"
            label="Loan id"
            error={fieldErrors["loan_id"]}
            hint="From the Loan book register (the loan detail drawer shows the id)."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                value={loanId}
                onChange={(event) => setLoanId(event.target.value)}
                disabled={open.isPending}
                spellCheck={false}
              />
            )}
          </FormField>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={open.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={open.isPending}>
              {open.isPending ? "Opening…" : "Initiate recovery…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Initiate recovery"
          confirmPhrase={confirmEntry.loan_id.slice(0, 8)}
          confirmLabel="Initiate recovery"
          pending={open.isPending}
          onConfirm={() => {
            if (!open.isPending) open.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This opens a collections case on loan{" "}
            {confirmEntry.loan_id.slice(0, 8)} and puts it on the recovery
            worklist. Every note added to the file is permanent (no edit, no
            delete — the file is evidence).
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
