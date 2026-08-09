"use client";

/**
 * Disbursement dialog (module 4 — the MONEY-MOVEMENT surface deferred to this batch): funds a committee-APPROVED application via
 * `POST /applications/{id}/disburse` (the P7 atomic contract,
 * loan_book:create).
 *
 * - The dialog fetches the application FRESH (record class, staleTime
 *   0) and offers the action ONLY while the stage is `approved` — the
 *   UI never offers what the API forbids (least disclosure). The committee
 *   decision is the approval step (P9 maker-checker voting); the
 *   disburse contract now ALSO enforces user-level SoD server-side
 *   neither the application's initiator (created_by, /0036) nor its committee recommender (recommended_by, 0037) can
 *   post the disbursement — both are 403. The guard here remains the
 *   typed confirmation + the server's stage transition; the 403 renders
 *   through the shared ErrorBanner.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit + disabled controls, `retry: 0`, one
 *   Idempotency-Key per logical intent (stable across retries, rotated
 *   when the channel changes). After a recorded disbursement the
 *   affordance is SPENT — the result panel replaces the action and the
 *   approved queue refetch drops the row (replay impossible).
 * - A 409 (stage moved underneath: someone else disbursed or the
 *   application was rejected) renders the shared ConflictBanner's
 *   explicit reload-and-re-enter flow — reload refetches the record and
 *   NEVER replays the write.
 * - The body carries the cash channel ONLY; the posting date and every
 *   figure are server-resolved. The result (loan id, txn ref, schedule)
 *   renders VERBATIM from the response (blocker (a)).
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import { fetchApplication } from "@/modules/applications/api";
import type { Product } from "@/modules/products/schemas";
import { disburseApplication } from "../api";
import {
  CASH_CHANNELS,
  CHANNEL_LABELS,
  cashChannelSchema,
  type CashChannel,
  type Disbursement,
} from "../schemas";
import { ScheduleTable } from "./ScheduleTable";
import styles from "./Loans.module.css";

export function DisburseDialog({
  applicationId,
  products,
  onClose,
}: Readonly<{
  applicationId: string;
  products: Product[];
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [channel, setChannel] = useState("");
  const [channelError, setChannelError] = useState<string>("");
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<Disbursement | null>(null);
  const [notice, setNotice] = useState<string>("");
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Record class (staleTime 0): NEVER a stale application record into a
  // money write — the stage below is the freshest available read.
  const detail = useQuery({
    queryKey: ["applications", "detail", applicationId],
    queryFn: () => fetchApplication(applicationId),
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

  const disburse = useMutation({
    mutationFn: (chosen: CashChannel) =>
      disburseApplication(
        applicationId,
        chosen,
        idempotencyKeyFor(
          keySlot.current,
          // Key material = intent + FRESHNESS (W59-2, the F3 class):
          // the version of the fresh (staleTime 0) application read
          // anchors the key to the record state the operator acted on.
          // Pure retries of one intent keep the key STABLE; a changed
          // channel OR a post-409 reload that bumped the record version
          // ROTATES it — an identical legitimate re-attempt after a
          // conflict can never be served a stale pinned outcome.
          JSON.stringify({
            op: "disburse",
            id: applicationId,
            recordVersion: detail.data?.version ?? null,
            channel: chosen,
          }),
        ),
      ),
    onSuccess: (recorded) => {
      setConfirming(false);
      // SPENT affordance: the result panel replaces the action; the queue
      // and register refetch server truth (the application left the
      // approved stage; the loan now exists).
      setResult(recorded);
      announce("Loan disbursed.");
      void queryClient.invalidateQueries({ queryKey: ["applications", "detail", applicationId] });
      void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["loans", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["portfolio", "summary"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirming(false);
      announce("Disbursement was not recorded.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Disburse loan" onClose={onClose} variant="dialog">
        <div className={styles.muted}>Loading…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Disburse loan" onClose={onClose} variant="dialog">
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const app = detail.data;
  const product = products.find((p) => p.id === app.product_id);
  const conflict = disburse.isError && isConflict(disburse.error);
  const disbursable = app.stage === "approved" && result === null;

  function reloadRecord() {
    // Explicit reload flow: refetch the application (its stage moved
    // underneath) and the queue behind this dialog; the stale write is
    // NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["applications", "detail", applicationId] });
    void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
    disburse.reset();
    setNotice("Record reloaded — re-check the application stage before acting again.");
    // Every async outcome is announced: the post-conflict
    // reload is an outcome, not a success (W59-4, the F5 class).
    announce("Record reloaded after the conflict — re-check the application stage.");
  }

  function armConfirmation() {
    if (disburse.isPending) return;
    const parsed = cashChannelSchema.safeParse(channel);
    if (!parsed.success) {
      setChannelError("Select the disbursement channel.");
      return;
    }
    setChannelError("");
    setConfirming(true);
  }

  return (
    <Modal title="Disburse loan" onClose={onClose} closeDisabled={disburse.isPending} variant="dialog">
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
            <span className={styles.mono}>{app.member_id}</span>
          )}
        </Kv>
        <Kv label="Product">
          {product !== undefined ? product.name : <span className={styles.mono}>{app.product_id}</span>}
        </Kv>
        <Kv label="Approved amount">{fmtKes(app.amount)}</Kv>
        <Kv label="Term">{app.term_months} months</Kv>
        <Kv label="Rate">{app.rate_pct}% (product-set)</Kv>
        <Kv label="Stage">{app.stage}</Kv>
      </div>

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={disburse.error} onReload={reloadRecord} />
      {disburse.isError && !conflict && <ErrorBanner error={disburse.error} />}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Disbursed · ref {result.txn_ref}</div>
          <Kv label="Loan record">
            <span className={styles.mono}>{result.loan_id}</span>
          </Kv>
          <div className={styles.subhead}>Server-computed repayment schedule</div>
          <ScheduleTable rows={result.schedule} />
          <div className={styles.formNote}>
            The schedule above came from the disbursement response — nothing was
            computed in this screen. The loan now appears in the register.
          </div>
        </div>
      )}

      {disbursable && (
        <>
          <FormField
            id="disburse-channel"
            label="Disbursement channel"
            error={channelError === "" ? undefined : channelError}
            hint="Funds move via the selected cash channel; the posting date and schedule are resolved by the server."
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={channel}
                onChange={(event) => setChannel(event.target.value)}
                disabled={disburse.isPending}
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
            <Button type="button" onClick={onClose} disabled={disburse.isPending}>
              Cancel
            </Button>
            <Button type="button" variant="primary" onClick={armConfirmation} disabled={disburse.isPending}>
              {disburse.isPending ? "Disbursing…" : "Disburse…"}
            </Button>
          </div>
        </>
      )}
      {!disbursable && result === null && (
        <div className={styles.formNote}>
          This application is not in the approved stage — disbursement is not
          offered (the server enforces the stage regardless).
        </div>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Disburse funds"
          confirmPhrase={app.id.slice(0, 8)}
          confirmLabel="Disburse funds"
          pending={disburse.isPending}
          onConfirm={() => {
            if (disburse.isPending) return;
            const parsed = cashChannelSchema.safeParse(channel);
            if (!parsed.success) return;
            disburse.mutate(parsed.data);
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This moves {fmtKes(app.amount)} to the member via{" "}
            {channel === "mpesa" || channel === "bank" ? CHANNEL_LABELS[channel] : channel}. The
            ledger posting, loan record and repayment schedule are created
            atomically by the server and cannot be undone from this screen.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
