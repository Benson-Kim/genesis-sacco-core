"use client";

/**
 * Request-export drawer (module 8 — `POST /exports`, reports:view).
 *
 * - The form offers ONLY the filters the chosen report declares
 *   (schemas.REPORT_FILTERS, mirroring the backend registry): ids and
 *   dates — the sole caller-suppliable scope (blocker (a)). No
 *   format, column, limit or storage control exists anywhere in this
 *   UI because none exists in the contract.
 * - EXACTLY ONE write per intent: pending short-circuit + disabled
 *   controls, `retry: 0` (the Providers mutation default), one
 *   Idempotency-Key per logical intent (stable across pure retries; rotated when the report/filters change, after an acknowledged 409 via the conflict epoch, and on every success via the intent counter so "Request another" of an identical scope is a NEW export job, never a server-side dedup into the first response). No typed ConfirmDangerModal: requesting an export
 *   moves no money and destroys nothing — the server audits every
 *   request/render/download regardless (blocker (f)).
 * - SPENT affordance: the result panel (the SERVER's frozen scope,
 *   column allow-list and as-of, verbatim) replaces the form.
 * - A 409 here is a CREATE-style conflict (a concurrent identical
 *   request holding the idempotency claim) — there is no export record
 *   to reload and re-enter, so it renders the least-disclosure
 *   ErrorBanner with an operator note, not the ConflictBanner (F-precedent). Nothing is retried or replayed either way.
 * - The id-filter pickers mount ONLY for operators holding the grant
 *   their list read requires (members:view for members/exits,
 *   transactions:view for declarations) — a stripped role fetches
 *   NOTHING and falls back to a plain UUID field (the server authorizes the export itself on reports:view alone; least disclosure).
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { fmtDateTime } from "@/lib/format";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { fetchMembersPage } from "@/modules/members/api";
import type { Member } from "@/modules/members/schemas";
import { fetchDeclarationsPage, fetchExitsPage, requestExport } from "../api";
import { recordWitnessedExport } from "../exportRegistry";
import {
  EMPTY_FILTER_DRAFT,
  REPORT_FILTERS,
  REPORT_LABELS,
  validateExportDraft,
  type DeclarationPickerRow,
  type ExitPickerRow,
  type ExportEntry,
  type ExportFilterDraft,
  type ExportOut,
  type FilterKey,
  type ReportName,
} from "../schemas";
import styles from "./Reports.module.css";

/**
 * Member picker — a separate component so its keyset hook mounts ONLY
 * for operators holding members:view (a stripped role fetches NOTHING; the useKeysetList primitive is consumed unmodified, reuse-first).
 */
function MemberPickerField({
  value,
  error,
  disabled,
  onChange,
}: Readonly<{
  value: string;
  error?: string;
  disabled: boolean;
  onChange: (memberId: string) => void;
}>) {
  const members = useKeysetList<Member>({
    queryKey: ["members", "list", { status: "", type: "" }],
    fetchPage: (cursor) => fetchMembersPage({ status: "", type: "" }, cursor),
  });
  const options = members.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <>
      <FormField id="exp-member" label="Member" error={error}>
        {(control) => (
          <select
            {...control}
            className={styles.select}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            disabled={disabled}
          >
            <option value="">Select a member…</option>
            {options.map((member) => (
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
    </>
  );
}

/** Exit-request picker — mounts ONLY with members:view (the
 * /member-exits list read is members-gated server-side). */
function ExitPickerField({
  value,
  error,
  disabled,
  onChange,
}: Readonly<{
  value: string;
  error?: string;
  disabled: boolean;
  onChange: (exitId: string) => void;
}>) {
  const exits = useKeysetList<ExitPickerRow>({
    queryKey: ["member-exits", "list", {}],
    fetchPage: (cursor) => fetchExitsPage(cursor),
  });
  const options = exits.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <>
      <FormField id="exp-exit" label="Exit request" error={error}>
        {(control) => (
          <select
            {...control}
            className={styles.select}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            disabled={disabled}
          >
            <option value="">Select an exit request…</option>
            {options.map((exit) => (
              <option key={exit.id} value={exit.id}>
                {exit.id.slice(0, 8)} · member {exit.member_id.slice(0, 8)} · {exit.status}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {exits.hasNextPage && (
        <Button
          type="button"
          onClick={() => void exits.fetchNextPage()}
          disabled={exits.isFetchingNextPage}
        >
          {exits.isFetchingNextPage ? "Loading…" : "Load more exits"}
        </Button>
      )}
    </>
  );
}

/** Declaration picker — mounts ONLY with transactions:view (the
 * /dividends/declarations list read is transactions-gated server-side). */
function DeclarationPickerField({
  value,
  error,
  disabled,
  onChange,
}: Readonly<{
  value: string;
  error?: string;
  disabled: boolean;
  onChange: (declarationId: string) => void;
}>) {
  const declarations = useKeysetList<DeclarationPickerRow>({
    queryKey: ["dividends", "list", {}],
    fetchPage: (cursor) => fetchDeclarationsPage(cursor),
  });
  const options = declarations.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <>
      <FormField id="exp-declaration" label="Dividend declaration" error={error}>
        {(control) => (
          <select
            {...control}
            className={styles.select}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            disabled={disabled}
          >
            <option value="">Select a declaration…</option>
            {options.map((declaration) => (
              <option key={declaration.id} value={declaration.id}>
                FY {declaration.fy_start} → {declaration.fy_end} · {declaration.status}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {declarations.hasNextPage && (
        <Button
          type="button"
          onClick={() => void declarations.fetchNextPage()}
          disabled={declarations.isFetchingNextPage}
        >
          {declarations.isFetchingNextPage ? "Loading…" : "Load more declarations"}
        </Button>
      )}
    </>
  );
}

/** Grant-stripped fallback: a plain UUID field, ZERO list fetches (the
 * export itself needs reports:view only — the server enforces). */
function UuidField({
  id,
  label,
  value,
  error,
  disabled,
  onChange,
}: Readonly<{
  id: string;
  label: string;
  value: string;
  error?: string;
  disabled: boolean;
  onChange: (next: string) => void;
}>) {
  return (
    <FormField
      id={id}
      label={label}
      error={error}
      hint="Your role cannot list these records — paste the record's UUID."
    >
      {(control) => (
        <input
          {...control}
          className={styles.input}
          maxLength={36}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled}
        />
      )}
    </FormField>
  );
}

export function RequestExportDrawer({
  report,
  onClose,
}: Readonly<{ report: ReportName; onClose: () => void }>) {
  const permissions = usePermissions();
  const [draft, setDraft] = useState<ExportFilterDraft>(EMPTY_FILTER_DRAFT);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [result, setResult] = useState<ExportOut | null>(null);
  // Freshness component (F3 /): after an acknowledged
  // 409 the NEXT attempt is a new intent — the epoch rotates the key
  // so a backend idempotency store pinned to the conflicted claim can
  // never serve its stale outcome. Pure 5xx retries keep the SAME key.
  const [conflictEpoch, setConflictEpoch] = useState(0);
  // Per-request intent counter: bumped on every SUCCESS so
  // "Request another" with an identical scope is a NEW export job —
  // without it the server would dedup the repeat into the first
  // response while the operator believes a fresh snapshot was queued.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const surface = REPORT_FILTERS[report];
  const mayPickMembers = can(permissions.data, "members", "view");
  const mayPickDeclarations = can(permissions.data, "transactions", "view");

  const request = useMutation({
    mutationFn: (entry: ExportEntry) =>
      requestExport(
        entry,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "export-request",
            entry,
            conflict_epoch: conflictEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (recorded) => {
      // SPENT affordance: the result panel replaces the form. The
      // witnessed registry keeps the record for refresh/download.
      recordWitnessedExport(recorded);
      setResult(recorded);
      setIntentSeq((seq) => seq + 1);
      announce("Export queued. Run the export queue to render it.");
    },
    onError: (error) => {
      if (isConflict(error)) setConflictEpoch((epoch) => epoch + 1);
      announce("The export was NOT requested.");
    },
  });

  const conflicted = request.isError && isConflict(request.error);
  const spent = result !== null;

  function submitDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (request.isPending || spent) return;
    const verdict = validateExportDraft(report, draft);
    if (verdict.errors !== null) {
      setClientErrors(verdict.errors);
      return;
    }
    setClientErrors({});
    request.mutate(verdict.entry);
  }

  function setFilter(key: FilterKey, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(request.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    request.error instanceof ApiError &&
    request.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  const offers = (key: FilterKey) => surface.allowed.includes(key);

  return (
    <Modal
      title="Request export"
      onClose={onClose}
      closeDisabled={request.isPending}
      // W56-3: a stray overlay click must never silently discard a
      // half-completed request — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      {request.isError && !renderedInline && <ErrorBanner error={request.error} />}
      {conflicted && (
        <div className={styles.formNote}>
          A conflict here means an identical export request is still being
          processed. Nothing was queued twice and nothing will be retried
          automatically — check the exports list before requesting again.
        </div>
      )}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Export queued · {REPORT_LABELS[report]}</div>
          <Kv label="Export id">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Status">{result.status}</Kv>
          <Kv label="As of">{fmtDateTime(result.as_of)}</Kv>
          <Kv label="Scope (frozen server-side)">
            {Object.keys(result.filters).length === 0 ? (
              "whole tenant"
            ) : (
              <span className={styles.mono}>
                {Object.entries(result.filters)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(" · ")}
              </span>
            )}
          </Kv>
          <Kv label="Columns (role allow-list)">{result.allowed_columns.join(", ")}</Kv>
          <div className={styles.formNote}>
            Everything above is the SERVER&apos;s frozen request record — the
            scope, the as-of cutoff and the column allow-list cannot be
            changed after this point. Run the export queue, then refresh the
            export&apos;s row on the Reports screen to pick up the CSV/PDF
            download links.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                // A NEW intent: the entry clears; an identical re-request
                // still rotates its key via the intent counter (T2).
                setResult(null);
                setDraft(EMPTY_FILTER_DRAFT);
                setClientErrors({});
                request.reset();
              }}
            >
              Request another
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitDraft} noValidate>
          <Banner>
            {REPORT_LABELS[report]}: the export renders server-side from a
            single snapshot with role-based columns, a hard row cap and an
            expiring download link. This screen only names the report and its
            declared id/date scope — nothing else can be sent.
          </Banner>

          {surface.allowed.length === 0 && (
            <div className={styles.formNote}>
              This report takes no filters — it always renders the whole
              tenant as of the server&apos;s snapshot time.
            </div>
          )}

          {offers("member_id") &&
            (mayPickMembers ? (
              <MemberPickerField
                value={draft.member_id}
                error={fieldErrors["member_id"]}
                disabled={request.isPending}
                onChange={(value) => setFilter("member_id", value)}
              />
            ) : (
              <UuidField
                id="exp-member"
                label="Member"
                value={draft.member_id}
                error={fieldErrors["member_id"]}
                disabled={request.isPending}
                onChange={(value) => setFilter("member_id", value)}
              />
            ))}

          {offers("exit_id") &&
            (mayPickMembers ? (
              <ExitPickerField
                value={draft.exit_id}
                error={fieldErrors["exit_id"]}
                disabled={request.isPending}
                onChange={(value) => setFilter("exit_id", value)}
              />
            ) : (
              <UuidField
                id="exp-exit"
                label="Exit request"
                value={draft.exit_id}
                error={fieldErrors["exit_id"]}
                disabled={request.isPending}
                onChange={(value) => setFilter("exit_id", value)}
              />
            ))}

          {offers("declaration_id") &&
            (mayPickDeclarations ? (
              <DeclarationPickerField
                value={draft.declaration_id}
                error={fieldErrors["declaration_id"]}
                disabled={request.isPending}
                onChange={(value) => setFilter("declaration_id", value)}
              />
            ) : (
              <UuidField
                id="exp-declaration"
                label="Dividend declaration"
                value={draft.declaration_id}
                error={fieldErrors["declaration_id"]}
                disabled={request.isPending}
                onChange={(value) => setFilter("declaration_id", value)}
              />
            ))}

          {offers("date_from") && (
            <FormField id="exp-from" label="From date" error={fieldErrors["date_from"]}>
              {(control) => (
                <input
                  {...control}
                  type="date"
                  className={styles.input}
                  value={draft.date_from}
                  onChange={(event) => setFilter("date_from", event.target.value)}
                  disabled={request.isPending}
                />
              )}
            </FormField>
          )}
          {offers("date_to") && (
            <FormField id="exp-to" label="To date" error={fieldErrors["date_to"]}>
              {(control) => (
                <input
                  {...control}
                  type="date"
                  className={styles.input}
                  value={draft.date_to}
                  onChange={(event) => setFilter("date_to", event.target.value)}
                  disabled={request.isPending}
                />
              )}
            </FormField>
          )}

          <div className={styles.formNote}>
            Format, columns, row caps and storage are server-owned; every
            export is audited (who, which report, what scope, how many rows).
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={request.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={request.isPending}>
              {request.isPending ? "Requesting…" : "Request export"}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
