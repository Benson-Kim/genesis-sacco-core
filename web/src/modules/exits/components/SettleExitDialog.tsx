"use client";

/**
 * Exit-settlement dialog (module 7 — the TERMINAL money movement of the member lifecycle): `POST /member-exits/{id}/settlement`
 * (members:approve) atomically posts the committee-APPROVED snapshot —
 * ledger postings, zeroed balances, guarantee release and terminal
 * member transition in ONE server transaction (P7/).
 *
 * - The dialog fetches the exit FRESH (record class, staleTime 0) and
 *   offers the action ONLY while the status is `approved` — the UI
 *   never offers what the API forbids (least disclosure).
 * - SEPARATION OF DUTIES (blocker (f)): the settle affordance mounts
 *   ONLY inside MakerCheckerPanel — the contract bans the
 *   initiator from posting their own settlement (403, keyed on the
 *   persisted requested_by). The maker identity is the CONTRACT's
 *   requested_by first (/ follow-up — server truth),
 *   with the per-tab registry as the fallback witness for
 *   unattributed (NULL) rows; controls are structurally withheld for
 *   the maker (no override prop exists); the server enforces
 *   regardless.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit + disabled controls, `retry: 0`, one
 *   Idempotency-Key per logical intent — key material folds the fresh
 *   record VERSION (also PINNED in the wire body — the approved-snapshot rule), the chosen channel and a post-409 reload
 *   epoch. After a posted settlement the affordance is SPENT — the
 *   result panel replaces the action.
 * - A 409 (snapshot drift: any component moved since approval — the
 *   server re-verifies under the full lock set and posts NOTHING)
 *   renders the shared ConflictBanner's explicit reload-and-re-enter
 *   flow with an INFORMATIONAL notice + announce(). The
 *   remedy for drift is voiding and re-requesting — stated to the
 *   operator; NOTHING is replayed.
 * - The body carries the pinned version and the CASH channel ONLY; no
 *   money parameter exists. Every result figure (txn ref, settled
 *   record) renders VERBATIM from the response (blocker (a)).
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { MakerCheckerPanel } from "@/modules/authz/components/MakerCheckerPanel";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import {
  CASH_CHANNELS,
  CHANNEL_LABELS,
  cashChannelSchema,
  type CashChannel,
} from "@/modules/transactions/schemas";
import { fetchExit, postExitSettlement } from "../api";
import { EXIT_MAKER_UNKNOWN, exitMakerOf } from "../makerRegistry";
import type { SettlementResult } from "../schemas";
import styles from "./Exits.module.css";

export function SettleExitDialog({
  exitId,
  onClose,
}: Readonly<{
  exitId: string;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const [channel, setChannel] = useState("");
  const [channelError, setChannelError] = useState<string>("");
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<SettlementResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload — a re-attempt after the
  // acknowledged drift is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Record class (staleTime 0): NEVER a stale snapshot into the
  // terminal money write — the version below is the freshest read.
  const detail = useQuery({
    queryKey: ["exits", "detail", exitId],
    queryFn: () => fetchExit(exitId),
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

  const settle = useMutation({
    // STRUCTURAL freshness guard (review): the TERMINAL money
    // write refuses to fire without the loaded fresh record — the
    // pinned version is read from the fresh detail data or the
    // mutation rejects before anything reaches the wire. No sentinel
    // (version 0, null) can ever be pinned into the body or the key
    // material.
    mutationFn: (chosen: CashChannel) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh exit record is not loaded — the settlement was NOT posted."),
        );
      }
      return postExitSettlement(
        exitId,
        fresh.version,
        chosen,
        idempotencyKeyFor(
          keySlot.current,
          // Key material = intent + FRESHNESS (W59-2, the F3
          // class): the version of the fresh (staleTime 0) exit read —
          // ALSO pinned in the wire body — anchors the key to the
          // snapshot the operator acted on; the reload epoch rotates it
          // after an acknowledged conflict. Pure retries of one intent
          // keep the key STABLE; a changed channel, a reloaded version
          // or a post-409 reload ROTATES it.
          JSON.stringify({
            op: "exit-settle",
            id: exitId,
            recordVersion: fresh.version,
            channel: chosen,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: (posted) => {
      setConfirming(false);
      // SPENT affordance: the result panel replaces the action; the
      // register and record refetch server truth (the exit is settled,
      // the member is exited).
      setResult(posted);
      setNotice("");
      announce("Exit settlement posted to the ledger.");
      void queryClient.invalidateQueries({ queryKey: ["exits", "detail", exitId] });
      void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
      if (memberId !== undefined) {
        void queryClient.invalidateQueries({ queryKey: ["members", "detail", memberId] });
      }
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirming(false);
      announce("The exit settlement was NOT posted.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Post exit settlement" onClose={onClose} variant="dialog" dismissOnOverlay={false}>
        <div className={styles.muted}>Loading exit record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Post exit settlement" onClose={onClose} variant="dialog" dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const conflict = settle.isError && isConflict(settle.error);
  const settleable = record.status === "approved" && result === null;

  // resolve the confirmation banner's channel label through the
  // SHARED cash-channel vocabulary (cashChannelSchema + CHANNEL_LABELS)
  // instead of a hardcoded two-value check — a future cash channel can
  // never render as its raw enum value here. The raw draft only shows
  // while nothing valid is selected (the confirmation cannot arm then).
  const parsedChannel = cashChannelSchema.safeParse(channel);
  const channelLabel = parsedChannel.success ? CHANNEL_LABELS[parsedChannel.data] : channel;

  // SERVER TRUTH FIRST (follow-up on /0036): requested_by
  // is the contract's initiator attribution; the per-tab witness is
  // only the fallback for unattributed (NULL) rows — nothing invented.
  const witnessedMakerId = exitMakerOf(record.id);
  const makerId = record.requested_by ?? witnessedMakerId;
  const ownId = getOwnUserId();
  const makerLabel =
    record.requested_by !== null
      ? record.requested_by === ownId
        ? "You initiated this exit request (server attribution)."
        : // Least disclosure: the bare staff UUID via the
          // short-id convention — never a name or email.
          `Initiating officer ${record.requested_by.slice(0, 8)} (server attribution).`
      : witnessedMakerId === EXIT_MAKER_UNKNOWN
        ? "Initiating officer — unattributed on the server record (attribution is never invented); no request witnessed by this tab."
        : witnessedMakerId === ownId
          ? "You initiated this exit request (witnessed by this tab)."
          : "Initiating officer (witnessed by this tab).";

  function reloadRecord() {
    // Explicit reload flow: refetch the record (a snapshot
    // component drifted since approval, or the status moved) and the
    // register; the stale posting is NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["exits", "detail", exitId] });
    void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    settle.reset();
    setNotice(
      "Record reloaded — nothing was posted. If the snapshot drifted since approval, void this request and raise a fresh one to capture current balances.",
    );
    announce("Record reloaded after the conflict — nothing was posted.");
  }

  function armConfirmation() {
    if (settle.isPending) return;
    // the typed confirmation cannot even ARM without the loaded
    // fresh record (belt to the mutationFn's structural braces).
    if (detail.data === undefined) return;
    const parsed = cashChannelSchema.safeParse(channel);
    if (!parsed.success) {
      setChannelError("Select the payout channel.");
      return;
    }
    setChannelError("");
    setConfirming(true);
  }

  return (
    <Modal
      title="Post exit settlement"
      onClose={onClose}
      closeDisabled={settle.isPending}
      variant="dialog"
      // W56-3: a stray overlay click must never discard the half-armed
      // terminal money write.
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling (W59-4, the F5 class). */}
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Member">
          {member.data !== undefined ? (
            <>
              {member.data.name} · {member.data.member_no}
            </>
          ) : (
            <span className={styles.mono}>{record.member_id}</span>
          )}
        </Kv>
        <Kv label="Status">{record.status}</Kv>
        <Kv label="Approved">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Shares">{fmtKes(record.shares_amount)}</Kv>
        <Kv label="Deposits">{fmtKes(record.deposits_amount)}</Kv>
        <Kv label="Loan balance (offset)">{fmtKes(record.loan_balance)}</Kv>
        <Kv label="Exit fees (tenant-configured)">{fmtKes(record.fees)}</Kv>
        <Kv label="Net payable">
          <span className={styles.netCell}>{fmtKes(record.net_payable)}</span>
        </Kv>
        <Kv label="Pinned record version">{record.version}</Kv>
      </div>

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={settle.error} onReload={reloadRecord} />
      {settle.isError && !conflict && <ErrorBanner error={settle.error} />}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Settlement posted{result.txn_ref !== null ? ` · ref ${result.txn_ref}` : ""}
          </div>
          <Kv label="Status">{result.exit.status}</Kv>
          <Kv label="Net paid">
            <span className={styles.netCell}>{fmtKes(result.exit.net_payable)}</span>
          </Kv>
          <Kv label="Settled at">{fmtDateTime(result.exit.settled_at)}</Kv>
          {result.exit.settlement_txn_id !== null && (
            <Kv label="Settlement ledger row">
              <span className={styles.mono}>{result.exit.settlement_txn_id}</span>
            </Kv>
          )}
          <div className={styles.formNote}>
            Every figure above came from the settlement response — nothing was
            computed in this screen. The member is now exited; the ledger row
            appears in the transactions register.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      )}

      {settleable && (
        <MakerCheckerPanel
          makerId={makerId}
          makerLabel={makerLabel}
          makerAt={fmtDateTime(record.created_at)}
          checkerActions={
            <>
              <FormField
                id="settle-channel"
                label="Payout channel"
                error={channelError === "" ? undefined : channelError}
                hint="The net payable moves via the selected cash channel; the posting date and every figure are resolved by the server."
              >
                {(control) => (
                  <select
                    {...control}
                    className={styles.select}
                    value={channel}
                    onChange={(event) => setChannel(event.target.value)}
                    disabled={settle.isPending}
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
                <Button type="button" onClick={onClose} disabled={settle.isPending}>
                  Cancel
                </Button>
                <Button
                  type="button"
                  variant="primary"
                  onClick={armConfirmation}
                  disabled={settle.isPending}
                >
                  {settle.isPending ? "Posting…" : "Post settlement…"}
                </Button>
              </div>
            </>
          }
        >
          <div className={styles.formNote}>
            Committee-approved settlement awaiting posting: net payable{" "}
            {fmtKes(record.net_payable)} (server snapshot, re-verified
            component-by-component at posting).
          </div>
        </MakerCheckerPanel>
      )}
      {!settleable && result === null && (
        <div className={styles.formNote}>
          This exit is not in the approved status — settlement posting is not
          offered (the server enforces the transition matrix regardless).
        </div>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Post exit settlement"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Post settlement"
          pending={settle.isPending}
          onConfirm={() => {
            if (settle.isPending) return;
            const parsed = cashChannelSchema.safeParse(channel);
            if (!parsed.success) return;
            settle.mutate(parsed.data);
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This TERMINALLY exits the member: the server atomically posts the
            approved snapshot (net payable {fmtKes(record.net_payable)}) via{" "}
            {channelLabel},
            zeroes their balances, releases guarantees and marks them exited.
            It cannot be undone from this screen. The write pins record
            version {record.version} — any drift posts NOTHING.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
