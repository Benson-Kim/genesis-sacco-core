"use client";

/**
 * Recovery-case drawer (the case file): the full
 * CaseOut record, the append-only case file (keyset notes) and the
 * case writes its status allows — assignment, staff dispositions
 * and THE single post-closure outcome note. Reads are loan_book:view; every write here is
 * loan_book:edit (the case-mutation grant).
 *
 * - FRESH RECORD READ (record class, staleTime 0): every write arms
 *   against the freshest available read; the version anchors the
 *   assign/disposition bodies and every key material.
 * - LEAST DISCLOSURE (addendum): NO balance, penalty or provision
 *   figure exists on this surface — the loan's money lives behind the
 *    loan screens for the entitled. Staff UUIDs (opened_by,
 *   assignee, note authors) render via the short-id convention —
 *   never a name or email; "Assign to me" uses the session's own id.
 * - DISPOSITIONS: only the STAFF-settable targets are offered (pause
 *   disputed / irrecoverable — reason REQUIRED; resume; close
 *   restructured — outcome note REQUIRED, written atomically
 *   server-side). closed_cured / closed_written_off are NOT offered:
 *   loan facts close those (the nightly pass) and the server refuses
 *   them (409).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`; assign/disposition key material
 *   folds the fresh record VERSION (also pinned on the wire) + reload
 *   epoch (rule 3); notes fold a per-SUCCESS intent counter (an
 *   identical follow-up note is a NEW intent — rule 4's repeat leg).
 * - A 409 (version drift, an illegal move through the single
 *   gatekeeper, an unassignable assignee, a second outcome note)
 *   renders the shared ConflictBanner's explicit reload-and-re-enter
 *   flow — NOTHING is replayed.
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal, Pill } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { loanClassPill } from "@/modules/loans/components/pills";
import { addCaseNote, addOutcomeNote, assignCase, fetchCase, fetchCaseNotesPage, setDisposition } from "../api";
import {
  DISPOSITION_TARGET_LABELS,
  STAFF_DISPOSITION_TARGETS,
  assignEntrySchema,
  dispositionEntrySchema,
  isLiveCaseStatus,
  noteEntrySchema,
  type CaseNote,
  type DispositionEntry,
} from "../schemas";
import { caseStatusPill } from "./pills";
import styles from "./Recovery.module.css";

/** Bare staff UUID under least disclosure (the short-id convention). */
function ShortId({ id }: Readonly<{ id: string }>) {
  return (
    <span className={styles.mono} title={id}>
      {id.slice(0, 8)}
    </span>
  );
}

export function CaseDetailDrawer({
  caseId,
  onClose,
}: Readonly<{
  caseId: string;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [assigneeId, setAssigneeId] = useState("");
  const [assigneeError, setAssigneeError] = useState("");
  const [target, setTarget] = useState("");
  const [dispositionReason, setDispositionReason] = useState("");
  const [dispositionNote, setDispositionNote] = useState("");
  const [dispositionErrors, setDispositionErrors] = useState<Record<string, string>>({});
  const [confirmDisposition, setConfirmDisposition] = useState<DispositionEntry | null>(null);
  const [noteText, setNoteText] = useState("");
  const [noteError, setNoteError] = useState("");
  const [outcomeText, setOutcomeText] = useState("");
  const [outcomeError, setOutcomeError] = useState("");
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency keys.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-note intent counter (rule 4's repeat leg): consecutive
  // IDENTICAL notes ("called, no answer" twice) are distinct intents.
  const [noteSeq, setNoteSeq] = useState(0);
  const assignKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const dispositionKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const noteKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const outcomeKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const detail = useQuery({
    queryKey: ["recovery", "case", caseId],
    queryFn: () => fetchCase(caseId),
    // Record class: NEVER a stale record into a versioned write.
    staleTime: STALE_TIME.record,
  });

  // The permanent case file: keyset notes (append-only, oldest first).
  const notes = useKeysetList<CaseNote>({
    queryKey: ["recovery", "case", caseId, "notes"],
    fetchPage: (cursor) => fetchCaseNotesPage(caseId, cursor),
  });
  const noteRows = notes.data?.pages.flatMap((page) => page.items) ?? [];

  const assign = useMutation({
    // STRUCTURAL freshness guard: the versioned write refuses
    // to fire without the loaded fresh record.
    mutationFn: (assignee: string) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh case record is not loaded — nothing was assigned."),
        );
      }
      return assignCase(
        caseId,
        fresh.version,
        assignee,
        idempotencyKeyFor(
          assignKeySlot.current,
          // Versioned-write material (README rule 3): the version is
          // ALSO pinned in the wire body.
          JSON.stringify({
            op: "recovery-case-assign",
            id: caseId,
            version: fresh.version,
            assignee_id: assignee,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: () => {
      setAssigneeId("");
      setNotice("");
      announce("Case assigned — recorded with before/after on the audit trail.");
      void queryClient.invalidateQueries({ queryKey: ["recovery", "case", caseId] });
      void queryClient.invalidateQueries({ queryKey: ["recovery", "worklist"] });
    },
    onError: () => {
      announce("The case was NOT assigned.");
    },
  });

  const disposition = useMutation({
    mutationFn: (entry: DispositionEntry) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh case record is not loaded — no disposition was recorded."),
        );
      }
      return setDisposition(
        caseId,
        fresh.version,
        entry,
        idempotencyKeyFor(
          dispositionKeySlot.current,
          JSON.stringify({
            op: "recovery-case-disposition",
            id: caseId,
            version: fresh.version,
            status: entry.status,
            reason: entry.reason,
            note: entry.note,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: (record, entry) => {
      setConfirmDisposition(null);
      setTarget("");
      setDispositionReason("");
      setDispositionNote("");
      setNotice("");
      announce(`Disposition recorded — the case is now ${record.status.replaceAll("_", " ")}.`);
      void queryClient.invalidateQueries({ queryKey: ["recovery", "case", caseId] });
      void queryClient.invalidateQueries({ queryKey: ["recovery", "worklist"] });
      if (entry.status === "closed_restructured") {
        // THE outcome note was written atomically with the close —
        // refresh the file so it appears.
        void queryClient.invalidateQueries({ queryKey: ["recovery", "case", caseId, "notes"] });
      }
    },
    onError: () => {
      setConfirmDisposition(null);
      announce("The disposition was NOT recorded.");
    },
  });

  const note = useMutation({
    mutationFn: (text: string) =>
      addCaseNote(
        caseId,
        text,
        idempotencyKeyFor(
          noteKeySlot.current,
          JSON.stringify({
            op: "recovery-case-note",
            id: caseId,
            note: text,
            reload_epoch: reloadEpoch,
            intent_seq: noteSeq,
          }),
        ),
      ),
    onSuccess: () => {
      setNoteText("");
      // The NEXT note is a new intent even if byte-identical.
      setNoteSeq((seq) => seq + 1);
      setNotice("");
      announce("Note appended to the case file (permanent).");
      void queryClient.invalidateQueries({ queryKey: ["recovery", "case", caseId, "notes"] });
    },
    onError: () => {
      announce("The note was NOT appended.");
    },
  });

  const outcome = useMutation({
    mutationFn: (text: string) =>
      addOutcomeNote(
        caseId,
        text,
        idempotencyKeyFor(
          outcomeKeySlot.current,
          // One outcome note per case, ever — no intent counter: a
          // retry of the same text reuses the key; the server's
          // partial UNIQUE is the authority.
          JSON.stringify({
            op: "recovery-case-outcome-note",
            id: caseId,
            note: text,
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: () => {
      setOutcomeText("");
      setNotice("");
      announce("Outcome note recorded — exactly one per case, permanent.");
      void queryClient.invalidateQueries({ queryKey: ["recovery", "case", caseId, "notes"] });
    },
    onError: () => {
      announce("The outcome note was NOT recorded.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Recovery case" onClose={onClose} dismissOnOverlay={false}>
        <div className={styles.muted}>Loading case record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Recovery case" onClose={onClose} dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const mayEdit = can(permissions.data, "loan_book", "edit");
  const live = isLiveCaseStatus(record.status);
  const anyPending =
    assign.isPending || disposition.isPending || note.isPending || outcome.isPending;
  const ownId = getOwnUserId();
  const hasOutcomeNote = noteRows.some((row) => row.is_outcome);

  function reloadRecord() {
    // Explicit reload flow: refetch the record and the file;
    // the stale write is NEVER replayed — re-entering it is a NEW
    // intent whose key rotates via the reload epoch.
    void queryClient.refetchQueries({ queryKey: ["recovery", "case", caseId] });
    void queryClient.invalidateQueries({ queryKey: ["recovery", "case", caseId, "notes"] });
    void queryClient.invalidateQueries({ queryKey: ["recovery", "worklist"] });
    setReloadEpoch((epoch) => epoch + 1);
    assign.reset();
    disposition.reset();
    note.reset();
    outcome.reset();
    setNotice("Record reloaded — re-check the case status before acting again.");
    announce("Record reloaded after the conflict — re-check the case status.");
  }

  function submitAssign(assignee: string) {
    if (assign.isPending) return;
    const parsed = assignEntrySchema.safeParse({ assignee_id: assignee.trim() });
    if (!parsed.success) {
      setAssigneeError(parsed.error.issues[0]?.message ?? "Enter the assignee id.");
      return;
    }
    setAssigneeError("");
    assign.mutate(parsed.data.assignee_id);
  }

  function armDisposition() {
    if (disposition.isPending) return;
    const parsed = dispositionEntrySchema.safeParse({
      status: target,
      reason: dispositionReason,
      note: dispositionNote,
    });
    if (!parsed.success) {
      const errors: Record<string, string> = {};
      for (const issue of parsed.error.issues) {
        const key = String(issue.path[0] ?? "status");
        if (errors[key] === undefined) errors[key] = issue.message;
      }
      setDispositionErrors(errors);
      return;
    }
    setDispositionErrors({});
    setConfirmDisposition(parsed.data);
  }

  function submitNote() {
    if (note.isPending) return;
    const parsed = noteEntrySchema.safeParse({ note: noteText.trim() });
    if (!parsed.success) {
      setNoteError(parsed.error.issues[0]?.message ?? "The note text is required.");
      return;
    }
    setNoteError("");
    note.mutate(parsed.data.note);
  }

  function submitOutcome() {
    if (outcome.isPending) return;
    const parsed = noteEntrySchema.safeParse({ note: outcomeText.trim() });
    if (!parsed.success) {
      setOutcomeError(parsed.error.issues[0]?.message ?? "The note text is required.");
      return;
    }
    setOutcomeError("");
    outcome.mutate(parsed.data.note);
  }

  return (
    <Modal
      title="Recovery case"
      onClose={onClose}
      closeDisabled={anyPending}
      // W56-3: this drawer bears versioned writes and permanent notes.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Status">{caseStatusPill(record.status)}</Kv>
        <Kv label="Case id">
          <span className={styles.mono}>{record.id}</span>
        </Kv>
        <Kv label="Loan">
          <ShortId id={record.loan_id} />
        </Kv>
        <Kv label="Opened by">
          {/* Contract attribution (the pattern): bare staff UUID,
              short-id convention — never resolved to a name/email. */}
          <ShortId id={record.opened_by} />
        </Kv>
        <Kv label="Assignee">
          {record.assignee_id !== null ? (
            record.assignee_id === ownId ? (
              <>you (<ShortId id={record.assignee_id} />)</>
            ) : (
              <ShortId id={record.assignee_id} />
            )
          ) : (
            "— (unassigned)"
          )}
        </Kv>
        <Kv label="Classification at open">{loanClassPill(record.classification_at_open)}</Kv>
        <Kv label="Days past due at open">
          <span className={styles.dpdCell}>{record.days_past_due_at_open}</span>
        </Kv>
        <Kv label="Opened">{fmtDateTime(record.opened_at)}</Kv>
        <Kv label="First assigned">{fmtDateTime(record.first_assigned_at)}</Kv>
        <Kv label="Closed">{fmtDateTime(record.closed_at)}</Kv>
        <Kv label="Record version">{record.version}</Kv>
      </div>
      <div className={styles.formNote}>
        No balances render here (least disclosure): the loan&apos;s figures
        live behind the Loan book screens, for the entitled. Cure and
        write-off close this case automatically from loan facts — never by
        hand.
      </div>

      {/* One copy of the 409 reload flow (reuse-first). */}
      <ConflictBanner error={assign.error} onReload={reloadRecord} />
      {assign.isError && !isConflict(assign.error) && <ErrorBanner error={assign.error} />}
      <ConflictBanner error={disposition.error} onReload={reloadRecord} />
      {disposition.isError && !isConflict(disposition.error) && (
        <ErrorBanner error={disposition.error} />
      )}
      <ConflictBanner error={note.error} onReload={reloadRecord} />
      {note.isError && !isConflict(note.error) && <ErrorBanner error={note.error} />}
      <ConflictBanner error={outcome.error} onReload={reloadRecord} />
      {outcome.isError && !isConflict(outcome.error) && <ErrorBanner error={outcome.error} />}

      {live && mayEdit && (
        <>
          <div className={styles.subhead}>Assignment</div>
          <div className={styles.formNote}>
            The assignee must be an ACTIVE user of this SACCO holding the
            collections grant; the Auditor is refused (audit independence) —
            all vetted server-side against the directory.
          </div>
          <div className={styles.assignRow}>
            <div className={styles.assignField}>
              <FormField
                id="case-assignee"
                label="Assignee user id"
                error={assigneeError === "" ? undefined : assigneeError}
                hint="The user's UUID from Access control (least disclosure: no directory is fetched here)."
              >
                {(control) => (
                  <input
                    {...control}
                    className={styles.input}
                    value={assigneeId}
                    onChange={(event) => setAssigneeId(event.target.value)}
                    disabled={anyPending}
                    spellCheck={false}
                  />
                )}
              </FormField>
            </div>
            <Button type="button" onClick={() => submitAssign(assigneeId)} disabled={anyPending}>
              {assign.isPending ? "Assigning…" : "Assign"}
            </Button>
            {ownId !== null && record.assignee_id !== ownId && (
              <Button type="button" onClick={() => submitAssign(ownId)} disabled={anyPending}>
                Assign to me
              </Button>
            )}
          </div>

          <div className={styles.subhead}>Disposition</div>
          <FormField
            id="case-disposition"
            label="Disposition"
            error={dispositionErrors["status"]}
            hint="Only staff-settable moves are offered; cured / written-off closes are job-only from loan facts."
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={target}
                onChange={(event) => setTarget(event.target.value)}
                disabled={anyPending}
              >
                <option value="">Select a disposition…</option>
                {STAFF_DISPOSITION_TARGETS.map((option) => (
                  <option key={option} value={option}>
                    {DISPOSITION_TARGET_LABELS[option]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          {(target === "disputed" || target === "irrecoverable_pending_write_off") && (
            <FormField
              id="case-disposition-reason"
              label="Reason (required to pause)"
              error={dispositionErrors["reason"]}
              hint="Captured into the audit record — the case stays LIVE and keeps blocking a duplicate case."
            >
              {(control) => (
                <textarea
                  {...control}
                  className={styles.textarea}
                  maxLength={500}
                  value={dispositionReason}
                  onChange={(event) => setDispositionReason(event.target.value)}
                  disabled={anyPending}
                />
              )}
            </FormField>
          )}
          {target === "closed_restructured" && (
            <FormField
              id="case-disposition-note"
              label="Closing outcome note (required)"
              error={dispositionErrors["note"]}
              hint="THE one outcome note, written atomically with the close — or nothing happens at all."
            >
              {(control) => (
                <textarea
                  {...control}
                  className={styles.textarea}
                  maxLength={2000}
                  value={dispositionNote}
                  onChange={(event) => setDispositionNote(event.target.value)}
                  disabled={anyPending}
                />
              )}
            </FormField>
          )}
          <div className={styles.actions}>
            <Button type="button" variant="primary" onClick={armDisposition} disabled={anyPending}>
              Record disposition…
            </Button>
          </div>
        </>
      )}
      {live && !mayEdit && (
        <div className={styles.formNote}>
          Your role has no loan book edit permission — assignment, disposition
          and note affordances are not offered.
        </div>
      )}
      {!live && (
        <div className={styles.formNote}>
          This case is closed ({record.status.replaceAll("_", " ")}) — assignment and
          dispositions are over; the file below is permanent. Exactly one
          closing outcome note may be added.
        </div>
      )}

      <div className={styles.subhead}>Case file (permanent — no edit, no delete)</div>
      {notes.isError && <ErrorBanner error={notes.error} />}
      {noteRows.length === 0 && !notes.isPending && !notes.isError && (
        <div className={styles.formNote}>No notes on the file yet.</div>
      )}
      {noteRows.map((row) => (
        <div key={row.id} className={styles.noteItem}>
          <div className={styles.noteMeta}>
            {row.is_outcome && (
              <Pill bg="navySoft" color="navy">
                Outcome note
              </Pill>
            )}
            <ShortId id={row.author_id} />
            <span>{fmtDateTime(row.created_at)}</span>
          </div>
          <div className={styles.noteBody}>{row.note}</div>
        </div>
      ))}
      {notes.hasNextPage && (
        <div className={styles.actions}>
          <Button
            type="button"
            onClick={() => void notes.fetchNextPage()}
            disabled={notes.isFetchingNextPage}
          >
            {notes.isFetchingNextPage ? "Loading…" : "Load more notes"}
          </Button>
        </div>
      )}

      {live && mayEdit && (
        <>
          <FormField
            id="case-note"
            label="Add note"
            error={noteError === "" ? undefined : noteError}
            hint="Every call, promise and visit goes on the record — permanently."
          >
            {(control) => (
              <textarea
                {...control}
                className={styles.textarea}
                maxLength={2000}
                value={noteText}
                onChange={(event) => setNoteText(event.target.value)}
                disabled={anyPending}
              />
            )}
          </FormField>
          <div className={styles.actions}>
            <Button type="button" onClick={submitNote} disabled={anyPending}>
              {note.isPending ? "Appending…" : "Append note"}
            </Button>
          </div>
        </>
      )}

      {!live && mayEdit && !hasOutcomeNote && (
        <>
          <FormField
            id="case-outcome-note"
            label="Closing outcome note (one per case)"
            error={outcomeError === "" ? undefined : outcomeError}
            hint="Exactly one, permanent, at/after closure only — the server refuses a second."
          >
            {(control) => (
              <textarea
                {...control}
                className={styles.textarea}
                maxLength={2000}
                value={outcomeText}
                onChange={(event) => setOutcomeText(event.target.value)}
                disabled={anyPending}
              />
            )}
          </FormField>
          <div className={styles.actions}>
            <Button type="button" onClick={submitOutcome} disabled={anyPending}>
              {outcome.isPending ? "Recording…" : "Record outcome note"}
            </Button>
          </div>
        </>
      )}

      {confirmDisposition !== null && (
        <ConfirmDangerModal
          title="Record disposition"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Record disposition"
          pending={disposition.isPending}
          onConfirm={() => {
            if (!disposition.isPending) disposition.mutate(confirmDisposition);
          }}
          onClose={() => setConfirmDisposition(null)}
        >
          <Banner>
            {confirmDisposition.status === "closed_restructured"
              ? "This CLOSES the case as restructured — the one staff-attested terminal — and writes THE closing outcome note in the same transaction. A closed case cannot be reopened."
              : confirmDisposition.status === "open"
                ? "This resumes the paused case to open — it becomes workable again on the worklist."
                : "This PAUSES the case with your reason on the audit record. The case stays live and keeps blocking a duplicate case; loan facts (cure or committee write-off) still close it automatically."}{" "}
            The write pins record version {record.version} — any drift is a
            conflict with nothing changed.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
