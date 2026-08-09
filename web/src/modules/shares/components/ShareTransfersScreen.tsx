"use client";

/**
 * Share-transfers console (the human-authorized maker-checker rework of the (e) console). Consumes EXACTLY the reworked contract:
 *
 * - MAKER phase `POST /members/{member_id}/share-transfers`
 *   (members:approve): creates a PENDING workflow record bound to the
 *   persisted approval snapshot — NO money moves from this form.
 * - The ledger-(m) history REGISTER `GET /share-transfers`
 *   (members:view): keyset, PENDING FIRST then newest first — the
 *   server's order, never re-sorted locally. A row opens the review
 *   drawer where a DIFFERENT principal approves or rejects
 *   (TransferDetailDrawer — the checker surface).
 *
 * Security posture (the corrections precedent):
 * - Route guard is members:view (RequireModule); the request form
 *   mounts ONLY with members:approve — pure UX; the server enforces
 *   every call (least disclosure, deny by default). Checker affordances live
 *   in the drawer behind MakerCheckerPanel (structural SoD withhold).
 * - MONEY (blocker (a)): the amount is the operation's SUBJECT — the
 *   typed decimal STRING end-to-end, never a float. Every rendered
 *   figure is the SERVER's, verbatim via fmtKes.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`, one Idempotency-Key per
 *   logical intent (README MATERIAL rule: canonical entry + reload
 *   epoch + per-SUCCESS intent counter — a legitimate identical
 *   second request is a NEW intent, never served the stored
 *   response).
 * - A 409 (insufficient balance under the locks, inactive member,
 *   self-transfer) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow — NOTHING is replayed.
 * - Every rendered string is attacker-influenced data; it renders
 *   exclusively through React text interpolation (gate-tested).
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Card, ConfirmDangerModal, Kv } from "@genesis/design-system";
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
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import grid from "@/modules/layout/grid.module.css";
import { fetchShareTransfersPage, requestShareTransfer } from "../api";
import {
  transferEntrySchema,
  type ShareTransferRecord,
  type TransferEntry,
} from "../schemas";
import { TransferDetailDrawer } from "./TransferDetailDrawer";
import { transferStatusPill } from "./pills";
import styles from "./Shares.module.css";

export function ShareTransfersScreen() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [fromMemberId, setFromMemberId] = useState("");
  const [toMemberId, setToMemberId] = useState("");
  const [amount, setAmount] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [confirmEntry, setConfirmEntry] = useState<TransferEntry | null>(null);
  const [requested, setRequested] = useState<ShareTransferRecord | null>(null);
  const [openTransferId, setOpenTransferId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload — a re-entered identical
  // request after a 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success intent counter (the lesson): a SECOND request of
  // the same amount between the same members is legitimate and is a
  // NEW intent — never served the first request's stored response.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // The ledger-(m) register: server-ordered keyset pages (pending
  // first — the checker's job order) — never re-sorted or filtered
  // locally.
  const register = useKeysetList<ShareTransferRecord>({
    queryKey: ["shares", "transfers-register"],
    fetchPage: (cursor) => fetchShareTransfersPage(cursor),
  });

  const request = useMutation({
    mutationFn: (entry: TransferEntry) =>
      requestShareTransfer(
        entry.from_member_id,
        entry.to_member_id,
        entry.amount,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "share-transfer-request",
            from: entry.from_member_id,
            to: entry.to_member_id,
            amount: entry.amount,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirmEntry(null);
      // SPENT affordance: the pending-record panel replaces the form.
      setRequested(record);
      // The NEXT request is a new intent even if byte-identical (T2).
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce("Transfer request recorded — PENDING a different checker's approval; no money moved.");
      void queryClient.invalidateQueries({ queryKey: ["shares", "transfers-register"] });
    },
    onError: () => {
      setConfirmEntry(null);
      announce("The transfer request was NOT recorded.");
    },
  });

  const mayRequest = can(permissions.data, "members", "approve");
  const conflict = request.isError && isConflict(request.error);
  const spent = requested !== null;

  const registerColumns: Column<ShareTransferRecord>[] = [
    {
      key: "status",
      header: "Status",
      render: (row) => transferStatusPill(row.status),
    },
    {
      key: "transfer",
      header: "Transfer",
      render: (row) => (
        <span className={styles.mono} title={row.id}>
          {row.id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "from",
      header: "From member",
      render: (row) => (
        <span className={styles.mono} title={row.from_member_id}>
          {row.from_member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "to",
      header: "To member",
      render: (row) => (
        <span className={styles.mono} title={row.to_member_id}>
          {row.to_member_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "amount",
      header: "Amount",
      align: "right",
      // The SERVER's figure, verbatim (blocker (a)).
      render: (row) => <span className={styles.amountCell}>{fmtKes(row.amount)}</span>,
    },
    {
      key: "maker",
      header: "Maker",
      // Bare staff UUID under least disclosure (short-id render);
      // a NULL maker is honest pre-workflow history, never invented.
      render: (row) =>
        row.created_by !== null ? (
          <span className={styles.mono} title={row.created_by}>
            {row.created_by.slice(0, 8)}
          </span>
        ) : (
          <span className={styles.muted}>—</span>
        ),
    },
    {
      key: "requested",
      header: "Requested",
      render: (row) => <span className={styles.muted}>{fmtDateTime(row.created_at)}</span>,
    },
  ];

  function reloadAfterConflict() {
    // Explicit reload flow: the conflicted attempt recorded
    // NOTHING (server-verified under the full lock set); the operator
    // re-checks the member's shares and re-enters. The stale intent is
    // NEVER replayed: its key rotates via the epoch.
    setReloadEpoch((epoch) => epoch + 1);
    request.reset();
    void queryClient.invalidateQueries({ queryKey: ["shares", "transfers-register"] });
    setNotice(
      "Nothing was recorded by the conflicted attempt. Re-check the transferring member's share balance and status (Members module) before re-entering — the request must clear the balance check under the server's row locks.",
    );
    announce("Conflict acknowledged — nothing was recorded; re-check the member before re-entering.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (request.isPending || spent) return;
    const parsed = transferEntrySchema.safeParse({
      from_member_id: fromMemberId.trim(),
      to_member_id: toMemberId.trim(),
      amount: amount.trim(),
    });
    if (!parsed.success) {
      setClientErrors(fromZodError(parsed.error));
      return;
    }
    setClientErrors({});
    setConfirmEntry(parsed.data);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(request.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    request.error instanceof ApiError &&
    request.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <div>
      <div className={grid.cards4}>
        <Card className={grid.wide}>
          <div className={styles.cardTitle}>Share transfers — maker-checker</div>
          <div className={styles.cardBody}>
            Moving share capital between members takes FOUR EYES
            (human-authorized): a maker requests and the system freezes
            the giver&apos;s balance; a DIFFERENT checker approves from the
            register below — the server re-verifies the frozen snapshot
            under the full lock set and refuses if anything moved, posting
            nothing. Both members are notified when a transfer posts. The
            register lists unfinished business first; every
            figure is the server&apos;s, verbatim.
          </div>
        </Card>
      </div>

      <Card>
        {notice !== "" && <Banner>{notice}</Banner>}

        {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
        <ConflictBanner error={request.error} onReload={reloadAfterConflict} />
        {request.isError && !conflict && !renderedInline && (
          <ErrorBanner error={request.error} />
        )}

        {spent && requested !== null && (
          <div className={styles.resultPanel} role="status">
            <div className={styles.resultTitle}>
              Transfer request recorded · PENDING approval
            </div>
            <Kv label="Transfer">
              <span className={styles.mono} title={requested.id}>
                {requested.id.slice(0, 8)}
              </span>
            </Kv>
            <Kv label="Status">{transferStatusPill(requested.status)}</Kv>
            <Kv label="Amount requested">
              <span className={styles.amountCell}>{fmtKes(requested.amount)}</span>
            </Kv>
            <div className={styles.formNote}>
              NO money moved: the request is bound to the giver&apos;s frozen
              balance and awaits a DIFFERENT checker&apos;s approval (you can
              never approve your own request — server-enforced and a
              database CHECK). It appears at the top of the register below.
            </div>
            <div className={styles.actions}>
              <Button
                type="button"
                variant="primary"
                onClick={() => {
                  // A NEW intent: entry cleared; fresh key material by
                  // content + intent_seq.
                  setRequested(null);
                  setFromMemberId("");
                  setToMemberId("");
                  setAmount("");
                  setClientErrors({});
                  request.reset();
                }}
              >
                Record another request
              </Button>
            </div>
          </div>
        )}

        {!spent && mayRequest && (
          <form onSubmit={submitEntry} noValidate>
            <FormField
              id="transfer-from"
              label="Transferring member id"
              error={fieldErrors["from_member_id"]}
              hint="The member whose shares move out (must be active; the server verifies)."
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  value={fromMemberId}
                  onChange={(event) => setFromMemberId(event.target.value)}
                  disabled={request.isPending}
                  spellCheck={false}
                />
              )}
            </FormField>
            <FormField
              id="transfer-to"
              label="Receiving member id"
              error={fieldErrors["to_member_id"]}
              hint="The active member receiving the shares."
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  value={toMemberId}
                  onChange={(event) => setToMemberId(event.target.value)}
                  disabled={request.isPending}
                  spellCheck={false}
                />
              )}
            </FormField>
            <FormField
              id="transfer-amount"
              label="Amount (KES)"
              error={fieldErrors["amount"]}
              hint="The share capital to move — the server refuses any amount exceeding the transferor's share balance."
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={18}
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                  disabled={request.isPending}
                />
              )}
            </FormField>
            <div className={styles.formNote}>
              This records a PENDING request only — no money moves until a
              DIFFERENT checker approves it from the register. The balance
              check, active-member checks and the frozen snapshot are all
              resolved by the server under the full lock set.
            </div>
            <div className={styles.actions}>
              <Button type="submit" variant="primary" disabled={request.isPending}>
                {request.isPending ? "Recording…" : "Request transfer…"}
              </Button>
            </div>
          </form>
        )}
        {!spent && !mayRequest && (
          <div className={styles.formNote}>
            Your role has no members approval permission — share-transfer
            requests move member equity and their controls are not offered.
            The server enforces this regardless.
          </div>
        )}
      </Card>

      <Card>
        <div className={styles.cardTitle}>Transfer register</div>
        <div className={styles.cardBody}>
          Pending requests first (the checker&apos;s job order), then the
          full history newest-first — the server&apos;s order, never
          re-sorted here. Open a row to review it; approval and rejection
          require a DIFFERENT principal from the maker.
        </div>
        <KeysetTable
          query={register}
          columns={registerColumns}
          rowKey={(row) => row.id}
          onRowClick={(row) => setOpenTransferId(row.id)}
          emptyMessage="No share transfers exist for this SACCO yet."
        />
      </Card>

      {openTransferId !== null && (
        <TransferDetailDrawer
          transferId={openTransferId}
          onClose={() => setOpenTransferId(null)}
        />
      )}

      {confirmEntry !== null && (
        <ConfirmDangerModal
          title="Request share transfer"
          confirmPhrase={confirmEntry.from_member_id.slice(0, 8)}
          confirmLabel="Request transfer"
          pending={request.isPending}
          onConfirm={() => {
            if (!request.isPending) request.mutate(confirmEntry);
          }}
          onClose={() => setConfirmEntry(null)}
        >
          <Banner>
            This records a PENDING request to move {fmtKes(confirmEntry.amount)}{" "}
            of share capital from member {confirmEntry.from_member_id.slice(0, 8)}{" "}
            to member {confirmEntry.to_member_id.slice(0, 8)}. NO money moves
            now: a DIFFERENT checker must approve it (you can never approve
            your own request), and the server re-verifies the frozen snapshot
            under row locks before anything posts.
          </Banner>
        </ConfirmDangerModal>
      )}
    </div>
  );
}
