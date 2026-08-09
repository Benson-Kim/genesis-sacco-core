"use client";

/**
 * Close-period dialog: `POST
 * /accounting-periods/close` (transactions:approve) freezes ledger
 * history for one fully elapsed calendar month.
 *
 * - The ONLY caller-chosen values are the calendar year and month —
 *   exactly the ClosePeriodBody contract; the period bounds, the
 *   "fully elapsed" rule and every posting-date check are resolved
 *   server-side (never caller-backdatable). No default
 *   month is pre-computed client-side: the approver chooses
 *   explicitly.
 * - SoD honesty: the contract gates this on transactions:approve as a
 *   DIRECT approver action — it is deliberately NOT maker-checker
 *   (the router's recorded P4 decision), so no MakerCheckerPanel is
 *   consumed; rendering a fake checker leg would claim a control the
 *   contract does not have.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase (the
 *   "YYYY-MM" month being frozen), pending short-circuit + disabled
 *   controls, one Idempotency-Key per logical intent. Key material
 *   folds the op discriminator + the full canonical body (README
 *   MATERIAL rule 1), a post-409 reload epoch and a
 *   per-success intent counter (W59-3) — there is no record version
 *   to fold: the (tenant, month) slot is the server's own idempotent
 *   claim (rule 3 has no version to pin here; the epoch/counter keep
 *   rotation honest).
 * - A 409 (month not fully elapsed, or already closed — possibly by a
 *   concurrent approver) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow: reload refetches the register with an
 *   INFORMATIONAL notice + announce(); the failed request is NEVER
 *   replayed.
 * - The result panel renders the SERVER's period row VERBATIM: bounds,
 *   status and close stamp are its facts, not the form's echo.
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { fmtDateTime } from "@/lib/format";
import { closePeriod } from "../api";
import {
  closePeriodEntrySchema,
  MONTH_NAMES,
  type ClosePeriodEntry,
  type PeriodRecord,
} from "../schemas";
import { periodStatusPill } from "./pills";
import styles from "./Periods.module.css";

/** The typed-confirmation phrase: the month being frozen, "YYYY-MM". */
function monthPhrase(entry: ClosePeriodEntry): string {
  return `${entry.year}-${String(entry.month).padStart(2, "0")}`;
}

export function ClosePeriodDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [year, setYear] = useState("");
  const [month, setMonth] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<ClosePeriodEntry | null>(null);
  const [result, setResult] = useState<PeriodRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload, so a re-entered identical
  // close after a 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success intent counter (W59-3): "Close another month" followed
  // by an identical entry is a NEW intent with a NEW key.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const close = useMutation({
    mutationFn: (entry: ClosePeriodEntry) =>
      closePeriod(
        entry.year,
        entry.month,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "period-close",
            body: { year: entry.year, month: entry.month },
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the result panel replaces the form.
      setResult(record);
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce("Accounting period closed — the month is frozen for posting.");
      // The register and every period-context note are server facts.
      void queryClient.invalidateQueries({ queryKey: ["periods", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["periods", "context"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirmEntry(null);
      announce("The accounting period was NOT closed.");
    },
  });

  const conflict = close.isError && isConflict(close.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the register (a concurrent
    // approver may have closed the month first); the failed request is
    // structurally WITHDRAWN — re-entering it is a NEW operator intent
    // whose key rotates via the reload epoch. NOTHING is replayed.
    void queryClient.refetchQueries({ queryKey: ["periods", "list"] });
    void queryClient.invalidateQueries({ queryKey: ["periods", "context"] });
    setReloadEpoch((epoch) => epoch + 1);
    close.reset();
    setNotice(
      "Register reloaded — the conflicted close was withdrawn, nothing was replayed. Check whether the month is already closed or not yet fully elapsed.",
    );
    announce("Register reloaded. The conflicted close was withdrawn; nothing was replayed.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (close.isPending || spent) return;
    // Shape-only client checks; the "fully elapsed" verdict is the
    // server's (422/409 rendered, never pre-empted beyond shape).
    const errors: Record<string, string> = {};
    const yearRaw = year.trim();
    if (!/^\d{4}$/.test(yearRaw)) {
      errors["year"] = "Enter a four-digit year between 2000 and 2100.";
    }
    if (month === "") {
      errors["month"] = "Choose the calendar month.";
    }
    if (Object.keys(errors).length > 0) {
      setClientErrors(errors);
      return;
    }
    const parsed = closePeriodEntrySchema.safeParse({
      year: Number(yearRaw),
      month: Number(month),
    });
    if (!parsed.success) {
      setClientErrors({ year: "Enter a four-digit year between 2000 and 2100." });
      return;
    }
    setClientErrors({});
    setConfirmEntry(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(close.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    close.error instanceof ApiError &&
    close.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <Modal
      title="Close accounting period"
      onClose={onClose}
      closeDisabled={close.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never discard the half-armed
      // ledger-freeze — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling (W59-4). */}
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={close.error} onReload={reloadAfterConflict} />
      {conflict && (
        <div className={styles.formNote}>
          Typical causes: the month is not fully elapsed yet (only elapsed
          months can be closed), or it is already closed — possibly by a
          concurrent approver. Nothing changed.
        </div>
      )}
      {close.isError && !conflict && !renderedInline && <ErrorBanner error={close.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Period closed · books frozen</div>
          <Kv label="Period">
            {result.period_start} → {result.period_end}
          </Kv>
          <Kv label="Close state">{periodStatusPill(result.status)}</Kv>
          <Kv label="Closed at">
            {result.closed_at === null ? "—" : fmtDateTime(result.closed_at)}
          </Kv>
          <div className={styles.formNote}>
            The bounds and the close stamp above are the SERVER&apos;s facts —
            nothing was computed in this screen. Postings dated inside this
            month are refused from now on (database-enforced).
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
                setYear("");
                setMonth("");
                setClientErrors({});
                close.reset();
              }}
            >
              Close another month
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <FormField
            id="period-year"
            label="Year"
            error={fieldErrors["year"]}
            hint="Four-digit calendar year (2000–2100)."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="numeric"
                maxLength={4}
                value={year}
                onChange={(event) => setYear(event.target.value)}
                disabled={close.isPending}
              />
            )}
          </FormField>
          <FormField id="period-month" label="Month" error={fieldErrors["month"]}>
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={month}
                onChange={(event) => setMonth(event.target.value)}
                disabled={close.isPending}
              >
                <option value="">Choose a month…</option>
                {MONTH_NAMES.map((name, index) => (
                  <option key={name} value={String(index + 1)}>
                    {name}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <div className={styles.formNote}>
            Only the calendar month travels — the period bounds, the
            &quot;fully elapsed&quot; rule and every posting-date check are the
            server&apos;s verdicts — never caller-backdatable. No
            figure is entered or computed here.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={close.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={close.isPending}>
              {close.isPending ? "Closing…" : "Close period…"}
            </Button>
          </div>
        </form>
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Confirm period close"
          confirmPhrase={monthPhrase(confirmEntry)}
          confirmLabel="Close period"
          pending={close.isPending}
          onConfirm={() => {
            if (!close.isPending) close.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This FREEZES the ledger for {MONTH_NAMES[confirmEntry.month - 1]}{" "}
            {confirmEntry.year} ({monthPhrase(confirmEntry)}): every posting
            dated inside the month is refused from the moment the close
            commits, and there is no reopen. The server verifies the month is
            fully elapsed before anything changes.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
