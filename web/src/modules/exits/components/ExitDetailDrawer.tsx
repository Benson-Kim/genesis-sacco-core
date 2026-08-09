"use client";

/**
 * Exit detail drawer (module 7): the FULL server settlement record
 * (ExitOut) plus the lifecycle writes this record's status allows —
 * committee votes and void. Settlement posting and the canonical
 * statement open their own surfaces from here (screen-level state).
 *
 * - FRESH RECORD READ (record class, staleTime 0): every lifecycle
 *   write arms against the freshest available read; the version below
 *   anchors the void write and the idempotency-key material.
 * - MONEY (blocker (a)): every snapshot figure (shares, deposits, loan
 *   balance, fees, net payable) is the SERVER's decimal string rendered
 *   verbatim via fmtKes — nothing is summed, netted or re-derived; a
 *   negative net payable renders with its sign (the documented
 *   loan-exceeds-assets branch).
 * - MAKER-CHECKER (blocker (f)): vote affordances mount ONLY inside
 *   MakerCheckerPanel — checker controls exist only for a different,
 *   known principal (getOwnUserId; no override prop). The maker
 *   identity is the CONTRACT's requested_by first (/ follow-up — server truth), with the per-tab registry as the
 *   fallback witness for unattributed (NULL) rows. The server bans
 *   self-votes and enforces one vote per voter regardless (least disclosure).
 *   VOID deliberately sits OUTSIDE the panel: the contract gates
 *   POST /member-exits/{id}/void on members:approve (verified against
 *   backend member_exits.py — ApproveCtx) and permits an approve-holder
 *   to void their OWN request (it moves no money and only narrows
 *   state — the documented request-withdrawal path). An initiator
 *   holding members:edit ONLY cannot void here NOR at the API — moot
 *   under the P4 matrix (edit and approve sit on the same roles), and
 *   the UI gate below mirrors the contract exactly (mayApprove).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`, key material folding intent +
 *   fresh record VERSION + reload epoch (+ the spent-vote registry
 *   keeping vote buttons withdrawn across remounts, W58-6).
 * - A 409 (someone voted/voided/settled underneath, or the version
 *   drifted) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow with an INFORMATIONAL notice + announce()
 *    — NOTHING is replayed.
 * - TERMINAL statuses (settled/rejected) structurally WITHDRAW every
 *   write affordance (the server enforces the transition matrix
 *   regardless).
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal, Pill } from "@genesis/design-system";
import { MakerCheckerPanel } from "@/modules/authz/components/MakerCheckerPanel";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import { fetchExit, voidExit, voteOnExit } from "../api";
import { EXIT_MAKER_UNKNOWN, exitMakerOf } from "../makerRegistry";
import { recordVotedExit, useHasVotedOnExit } from "../votedRegistry";
import { isTerminalExitStatus, type ExitRecord, type ExitVote, type ExitVoteResult } from "../schemas";
import { exitStatusPill } from "./pills";
import styles from "./Exits.module.css";

export function ExitDetailDrawer({
  exitId,
  onSettle,
  onViewStatement,
  onClose,
}: Readonly<{
  exitId: string;
  /** Open the settlement dialog for this APPROVED record. */
  onSettle: (record: ExitRecord) => void;
  /** Open the canonical statement document. */
  onViewStatement: () => void;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [confirmVote, setConfirmVote] = useState<ExitVote | null>(null);
  const [confirmVoid, setConfirmVoid] = useState(false);
  const [voteResult, setVoteResult] = useState<ExitVoteResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency keys: bumped on
  // every explicit post-conflict reload — a re-entered identical action
  // after a 409 is a NEW intent with a NEW key (some conflicts, e.g.
  // "someone else voted", do not bump the record version).
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const voteKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const voidKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  // Spent affordance across remounts (W58-6): closing and reopening the
  // drawer must not re-arm the vote buttons after a recorded vote.
  const alreadyVoted = useHasVotedOnExit(exitId);

  const detail = useQuery({
    queryKey: ["exits", "detail", exitId],
    queryFn: () => fetchExit(exitId),
    // Record class: NEVER a stale record into a lifecycle write.
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

  const vote = useMutation({
    // STRUCTURAL freshness guard (the F-M1 class, applied
    // module-wide): the vote never fires without the loaded fresh
    // record — its key material folds the REAL record version, never
    // a null sentinel.
    mutationFn: (choice: ExitVote) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh exit record is not loaded — the vote was NOT recorded."),
        );
      }
      return voteOnExit(
        exitId,
        choice,
        idempotencyKeyFor(
          voteKeySlot.current,
          JSON.stringify({
            op: "exit-vote",
            id: exitId,
            choice,
            recordVersion: fresh.version,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: (tally) => {
      setConfirmVote(null);
      setVoteResult(tally);
      // The vote is SPENT for this tab (W58-6): the per-tab registry
      // keeps the affordance withdrawn across drawer remounts.
      recordVotedExit(exitId);
      setNotice("");
      announce(
        tally.decision === null
          ? "Exit vote recorded — awaiting quorum."
          : "Committee decision reached on the exit request.",
      );
      // The vote may have decided the exit: refresh the record and the
      // register (the tally/decision/status are SERVER facts).
      void queryClient.invalidateQueries({ queryKey: ["exits", "detail", exitId] });
      void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
    },
    onError: () => {
      setConfirmVote(null);
      announce("The exit vote was NOT recorded.");
    },
  });

  const voidMutation = useMutation({
    mutationFn: (version: number) =>
      voidExit(
        exitId,
        version,
        idempotencyKeyFor(
          voidKeySlot.current,
          JSON.stringify({
            op: "exit-void",
            id: exitId,
            version,
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: () => {
      setConfirmVoid(false);
      setNotice("");
      announce("Exit request voided.");
      // The record narrowed to rejected — refetch server truth.
      void queryClient.invalidateQueries({ queryKey: ["exits", "detail", exitId] });
      void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
    },
    onError: () => {
      setConfirmVoid(false);
      announce("The exit request was NOT voided.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Exit request" onClose={onClose} dismissOnOverlay={false}>
        <div className={styles.muted}>Loading exit record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Exit request" onClose={onClose} dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const mayApprove = can(permissions.data, "members", "approve");
  const terminal = isTerminalExitStatus(record.status);
  const anyPending = vote.isPending || voidMutation.isPending;
  const voteConflict = vote.isError && isConflict(vote.error);
  const voidConflict = voidMutation.isError && isConflict(voidMutation.error);

  // SERVER TRUTH FIRST (follow-up on /0036): requested_by
  // is the contract's initiator attribution — the maker-checker record
  // itself, not a per-tab witness. The registry (the request THIS TAB
  // witnessed) is consulted only when the server record is
  // unattributed (requested_by === null); nothing is ever invented.
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
    // Explicit reload flow: refetch the record (its status,
    // version or tally moved underneath) and the register; the stale
    // write is NEVER replayed — re-entering it is a NEW intent whose
    // key rotates via the reload epoch.
    void queryClient.refetchQueries({ queryKey: ["exits", "detail", exitId] });
    void queryClient.invalidateQueries({ queryKey: ["exits", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    vote.reset();
    voidMutation.reset();
    setNotice("Record reloaded — re-check the exit status before acting again.");
    announce("Record reloaded after the conflict — re-check the exit status.");
  }

  return (
    <Modal
      title="Exit request"
      onClose={onClose}
      closeDisabled={anyPending}
      // W56-3: this drawer bears lifecycle-write affordances — a stray
      // overlay click must never discard an in-progress governance flow.
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling: the notice reports a
          post-conflict reload (W59-4, the F5 class). */}
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
        <Kv label="Status">{exitStatusPill(record.status)}</Kv>
        <Kv label="Reason">{record.reason ?? "—"}</Kv>
        <Kv label="Requested by">
          {/* Initiator attribution (/ follow-up): the
              SERVER's bare staff UUID, short-id convention — least
              disclosure: no name/email is ever fetched for it.
              NULL renders the honest unattributed affordance:
              an actor is never invented. */}
          {record.requested_by !== null ? (
            <span className={styles.mono} title={record.requested_by}>
              {record.requested_by.slice(0, 8)}
            </span>
          ) : (
            "— (unattributed)"
          )}
        </Kv>
        <Kv label="Requested">{fmtDateTime(record.created_at)}</Kv>
        <Kv label="Decided">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Settled">{fmtDateTime(record.settled_at)}</Kv>
        <Kv label="Record version">{record.version}</Kv>
      </div>

      <div className={styles.subhead}>Settlement snapshot (server-computed)</div>
      <div className={styles.detailGrid}>
        <Kv label="Shares">{fmtKes(record.shares_amount)}</Kv>
        <Kv label="Deposits">{fmtKes(record.deposits_amount)}</Kv>
        <Kv label="Loan balance (offset)">{fmtKes(record.loan_balance)}</Kv>
        <Kv label="Exit fees (tenant-configured)">{fmtKes(record.fees)}</Kv>
        <Kv label="Net payable">
          <span className={styles.netCell}>{fmtKes(record.net_payable)}</span>
        </Kv>
        {record.settlement_txn_id !== null && (
          <Kv label="Settlement ledger row">
            <span className={styles.mono}>{record.settlement_txn_id}</span>
          </Kv>
        )}
      </div>
      <div className={styles.formNote}>
        Every figure above is the SERVER&apos;s snapshot, computed under the P12
        lock set — nothing is summed, netted or re-derived in this screen. A
        negative net payable means the loan balance exceeds the member&apos;s
        assets (a documented settlement branch). Posting re-verifies the
        snapshot component-by-component and returns a conflict on any drift.
      </div>

      {voteResult !== null && (
        <div className={styles.tally} role="status">
          <Pill bg="emeraldSoft" color="emerald">
            {voteResult.approvals} approve
          </Pill>
          <Pill bg="brickSoft" color="brick">
            {voteResult.rejections} reject
          </Pill>
          {voteResult.decision !== null ? (
            <Pill
              bg={voteResult.decision === "approved" ? "emeraldSoft" : "brickSoft"}
              color={voteResult.decision === "approved" ? "emerald" : "brick"}
            >
              Decision: {voteResult.decision}
            </Pill>
          ) : (
            <Pill>Awaiting quorum</Pill>
          )}
        </div>
      )}

      {/* One copy of the 409 reload flow (reuse-first): a moved status,
          drifted version or already-recorded vote is reloaded, NEVER
          replayed. */}
      <ConflictBanner error={vote.error} onReload={reloadRecord} />
      {vote.isError && !voteConflict && <ErrorBanner error={vote.error} />}
      <ConflictBanner error={voidMutation.error} onReload={reloadRecord} />
      {voidMutation.isError && !voidConflict && <ErrorBanner error={voidMutation.error} />}

      {terminal && (
        <div className={styles.formNote}>
          This exit is {record.status} — a terminal state. No further
          lifecycle action exists for it (the server enforces the transition
          matrix regardless).
        </div>
      )}

      {!terminal && record.status === "requested" && (
        <>
          <div className={styles.subhead}>Committee decision</div>
          <MakerCheckerPanel
            makerId={makerId}
            makerLabel={makerLabel}
            makerAt={fmtDateTime(record.created_at)}
            checkerActions={
              mayApprove ? (
                <>
                  <div className={styles.formNote}>
                    One vote per committee member and no vote on your own
                    request (both server-enforced); the quorum is resolved
                    server-side at vote time.
                  </div>
                  {alreadyVoted && voteResult === null && (
                    <div className={styles.formNote}>
                      This tab already recorded your vote on this exit — one
                      vote per committee member; the buttons stay spent.
                    </div>
                  )}
                  <div className={styles.voteActions}>
                    <Button
                      variant="danger"
                      onClick={() => setConfirmVote("reject")}
                      disabled={anyPending || voteResult !== null || alreadyVoted}
                    >
                      Vote reject
                    </Button>
                    <Button
                      variant="primary"
                      onClick={() => setConfirmVote("approve")}
                      disabled={anyPending || voteResult !== null || alreadyVoted}
                    >
                      Vote approve
                    </Button>
                  </div>
                </>
              ) : (
                <div className={styles.formNote}>
                  Your role has no members approval permission — exit voting
                  controls are not offered.
                </div>
              )
            }
          >
            <div className={styles.formNote}>
              Awaiting committee decision on a net payable of{" "}
              {fmtKes(record.net_payable)} (server snapshot).
            </div>
          </MakerCheckerPanel>
        </>
      )}

      {!terminal && record.status === "approved" && (
        <div className={styles.formNote}>
          The committee approved this snapshot. Posting the settlement moves
          money atomically and re-verifies every component under the full
          lock set.
        </div>
      )}

      <div className={styles.actions}>
        <Button type="button" onClick={onViewStatement} disabled={anyPending}>
          View exit statement
        </Button>
        {/* VOID: offered on any OPEN exit to members:approve holders
            ONLY — the contract gate on POST /member-exits/{id}/void is
            members:approve (backend member_exits.py, ApproveCtx), and
            an approve-holding initiator may void their OWN request (it
            moves no money and only narrows state — the documented
            request-withdrawal path). An edit-only initiator cannot
            void, in this UI or at the API (moot today: the P4 matrix
            grants edit and approve to the same roles). */}
        {!terminal && mayApprove && (
          <Button
            type="button"
            variant="danger"
            onClick={() => setConfirmVoid(true)}
            disabled={anyPending}
          >
            Void request…
          </Button>
        )}
        {!terminal && record.status === "approved" && mayApprove && (
          <Button
            type="button"
            variant="primary"
            onClick={() => onSettle(record)}
            disabled={anyPending}
          >
            Post settlement…
          </Button>
        )}
      </div>

      {confirmVote !== null && (
        <ConfirmDangerModal
          title={confirmVote === "approve" ? "Cast approve vote" : "Cast reject vote"}
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel={
            confirmVote === "approve" ? "Confirm approve vote" : "Confirm reject vote"
          }
          pending={vote.isPending}
          onConfirm={() => {
            if (!vote.isPending) vote.mutate(confirmVote);
          }}
          onClose={() => setConfirmVote(null)}
        >
          <Banner>
            Your vote is recorded once and cannot be changed (one vote per
            committee member). A quorum of votes decides the exit — approval
            authorises posting the TERMINAL settlement of{" "}
            {fmtKes(record.net_payable)} (server snapshot).
          </Banner>
        </ConfirmDangerModal>
      )}

      {confirmVoid && (
        <ConfirmDangerModal
          title="Void exit request"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Void request"
          pending={voidMutation.isPending}
          onConfirm={() => {
            if (!voidMutation.isPending) voidMutation.mutate(record.version);
          }}
          onClose={() => setConfirmVoid(false)}
        >
          <Banner>
            This voids the open exit request (state narrows to rejected; no
            money moves) and frees the member&apos;s one-open-exit slot so a
            fresh request can capture current balances. The write pins record
            version {record.version} — any drift is a conflict with nothing
            changed.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
