"use client";

/**
 * Declaration detail drawer: the FULL server
 * declaration snapshot (DeclarationOut) plus the lifecycle
 * writes this record's status allows — committee votes and void.
 * Distribution opens its own dialog from here (screen-level state).
 *
 * - FRESH RECORD READ (record class, staleTime 0): every lifecycle
 *   write arms against the freshest available read; the version below
 *   anchors the void write and the idempotency-key material.
 * - MONEY (blocker (a)): every snapshot figure is the SERVER's decimal
 *   string rendered verbatim via fmtKes — nothing is summed or
 *   re-derived (payout = dividend + rebate is the DB's CHECK).
 * - MAKER-CHECKER (blocker (f)): vote affordances mount ONLY inside
 *   MakerCheckerPanel — checker controls exist only for a different,
 *   known principal (getOwnUserId; no override prop). The maker
 *   identity is the CONTRACT's `requested_by` FIRST (server truth, the exits /0036 precedent); the per-tab
 *   witness (makerRegistry.ts) is consulted ONLY for unattributed
 *   rows (requested_by === null) — attribution is never invented. The
 *   server bans the declarer's self-vote and enforces one vote per
 *   voter regardless (least disclosure).
 *   VOID deliberately sits OUTSIDE the panel (the exits precedent):
 *   the contract gates POST.../void on transactions:approve (verified
 *   against backend api/dividends.py — TxnApproveCtx) and permits an
 *   approve-holder to void their OWN declaration (it moves no money
 *   and only narrows state — the documented drift remedy that frees
 *   the FY slot); the UI gate below mirrors the contract exactly.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`, key material folding intent +
 *   fresh record VERSION + reload epoch (+ the spent-vote registry
 *   keeping vote buttons withdrawn across remounts, W58-6).
 * - A 409 (someone voted/voided/distributed underneath, or the version
 *   drifted) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow with an INFORMATIONAL notice + announce()
 *    — NOTHING is replayed.
 * - TERMINAL statuses (rejected/distributed) structurally WITHDRAW
 *   every write affordance (the server enforces the transition matrix
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
import { fetchDeclaration, voidDeclaration, voteOnDeclaration } from "../api";
import { DIVIDEND_MAKER_UNKNOWN, dividendMakerOf } from "../makerRegistry";
import { recordVotedDeclaration, useHasVotedOnDeclaration } from "../votedRegistry";
import {
  isTerminalDeclarationStatus,
  type DeclarationRecord,
  type DividendVote,
  type DividendVoteResult,
} from "../schemas";
import { declarationStatusPill } from "./pills";
import styles from "./Dividends.module.css";

export function DeclarationDetailDrawer({
  declarationId,
  onDistribute,
  onClose,
}: Readonly<{
  declarationId: string;
  /** Open the distribution dialog for this APPROVED record. */
  onDistribute: (record: DeclarationRecord) => void;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [confirmVote, setConfirmVote] = useState<DividendVote | null>(null);
  const [confirmVoid, setConfirmVoid] = useState(false);
  const [voteResult, setVoteResult] = useState<DividendVoteResult | null>(null);
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
  const alreadyVoted = useHasVotedOnDeclaration(declarationId);

  const detail = useQuery({
    queryKey: ["dividends", "detail", declarationId],
    queryFn: () => fetchDeclaration(declarationId),
    // Record class: NEVER a stale record into a lifecycle write.
    staleTime: STALE_TIME.record,
  });

  const vote = useMutation({
    // STRUCTURAL freshness guard (the F-M1 class, applied module-wide):
    // the vote never fires without the loaded fresh record — its key
    // material folds the REAL record version, never a null sentinel.
    mutationFn: (choice: DividendVote) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh declaration is not loaded — the vote was NOT recorded."),
        );
      }
      return voteOnDeclaration(
        declarationId,
        choice,
        idempotencyKeyFor(
          voteKeySlot.current,
          JSON.stringify({
            op: "dividend-vote",
            id: declarationId,
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
      recordVotedDeclaration(declarationId);
      setNotice("");
      announce(
        tally.decision === null
          ? "Dividend vote recorded — awaiting quorum."
          : "Committee decision reached on the dividend declaration.",
      );
      // The vote may have decided the declaration: refresh the record
      // and the register (tally/decision/status are SERVER facts).
      void queryClient.invalidateQueries({ queryKey: ["dividends", "detail", declarationId] });
      void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
    },
    onError: () => {
      setConfirmVote(null);
      announce("The dividend vote was NOT recorded.");
    },
  });

  const voidMutation = useMutation({
    mutationFn: (version: number) =>
      voidDeclaration(
        declarationId,
        version,
        idempotencyKeyFor(
          voidKeySlot.current,
          JSON.stringify({
            op: "dividend-void",
            id: declarationId,
            version,
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: () => {
      setConfirmVoid(false);
      setNotice("");
      announce("Dividend declaration voided.");
      // The record narrowed to rejected — refetch server truth.
      void queryClient.invalidateQueries({ queryKey: ["dividends", "detail", declarationId] });
      void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
    },
    onError: () => {
      setConfirmVoid(false);
      announce("The dividend declaration was NOT voided.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Dividend declaration" onClose={onClose} dismissOnOverlay={false}>
        <div className={styles.muted}>Loading declaration…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Dividend declaration" onClose={onClose} dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const mayApprove = can(permissions.data, "transactions", "approve");
  const terminal = isTerminalDeclarationStatus(record.status);
  const anyPending = vote.isPending || voidMutation.isPending;
  const voteConflict = vote.isError && isConflict(vote.error);
  const voidConflict = voidMutation.isError && isConflict(voidMutation.error);

  // SERVER TRUTH FIRST (the exits /0036
  // precedent): requested_by is the contract's declarer attribution —
  // the maker-checker record itself, not a per-tab witness. The
  // registry (the declaration THIS TAB witnessed) is consulted only
  // when the server record is unattributed (requested_by === null);
  // nothing is ever invented. The server 403s the declarer's
  // self-vote and self-distribution on the PERSISTED column
  // regardless (least disclosure).
  const witnessedMakerId = dividendMakerOf(record.id);
  const makerId = record.requested_by ?? witnessedMakerId;
  const ownId = getOwnUserId();
  const makerLabel =
    record.requested_by !== null
      ? record.requested_by === ownId
        ? "You declared this dividend (server attribution)."
        : // Least disclosure: the bare staff UUID via the short-id
          // convention — never a name or email.
          `Declaring officer ${record.requested_by.slice(0, 8)} (server attribution).`
      : witnessedMakerId === DIVIDEND_MAKER_UNKNOWN
        ? "Declaring officer — unattributed on the server record (attribution is never invented); no declaration witnessed by this tab. The server still refuses the declarer's own vote."
        : witnessedMakerId === ownId
          ? "You declared this dividend (witnessed by this tab)."
          : "Declaring officer (witnessed by this tab).";

  function reloadRecord() {
    // Explicit reload flow: refetch the record (its status,
    // version or tally moved underneath) and the register; the stale
    // write is NEVER replayed — re-entering it is a NEW intent whose
    // key rotates via the reload epoch.
    void queryClient.refetchQueries({ queryKey: ["dividends", "detail", declarationId] });
    void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    vote.reset();
    voidMutation.reset();
    setNotice("Record reloaded — re-check the declaration status before acting again.");
    announce("Record reloaded after the conflict — re-check the declaration status.");
  }

  return (
    <Modal
      title="Dividend declaration"
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
        <Kv label="Declaration">
          <span className={styles.mono} title={record.id}>
            {record.id.slice(0, 8)}
          </span>
        </Kv>
        <Kv label="Status">{declarationStatusPill(record.status)}</Kv>
        <Kv label="Financial year">
          {record.fy_start} → {record.fy_end}
        </Kv>
        <Kv label="Declared by">
          {/* Declarer attribution: the
              SERVER's bare staff UUID, short-id convention — least
              disclosure: no name/email is ever fetched for it. NULL
              renders the honest unattributed affordance: an actor is
              never invented. */}
          {record.requested_by !== null ? (
            <span className={styles.mono} title={record.requested_by}>
              {record.requested_by.slice(0, 8)}
            </span>
          ) : (
            "— (unattributed)"
          )}
        </Kv>
        <Kv label="Declared">{fmtDateTime(record.created_at)}</Kv>
        <Kv label="Decided">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Distributed">{fmtDateTime(record.distributed_at)}</Kv>
        <Kv label="Record version">{record.version}</Kv>
      </div>

      <div className={styles.subhead}>Payout snapshot (server-computed, tenant-configured rates)</div>
      <div className={styles.detailGrid}>
        <Kv label="Dividend rate">{record.dividend_rate_pct}% p.a. (config)</Kv>
        <Kv label="Rebate rate">{record.rebate_rate_pct}% p.a. (config)</Kv>
        <Kv label="Eligible members (incl. dormant)">{record.eligible_members}</Kv>
        <Kv label="Share basis">{fmtKes(record.total_share_basis)}</Kv>
        <Kv label="Deposit basis">{fmtKes(record.total_deposit_basis)}</Kv>
        <Kv label="Dividend">{fmtKes(record.total_dividend)}</Kv>
        <Kv label="Rebate">{fmtKes(record.total_rebate)}</Kv>
        <Kv label="Total payout">
          <span className={styles.payoutCell}>{fmtKes(record.total_payout)}</span>
        </Kv>
      </div>
      <div className={styles.formNote}>
        Every figure above is the SERVER&apos;s snapshot at declaration time —
        nothing is summed or re-derived in this screen (payout = dividend +
        rebate is the database&apos;s CHECK). Dormant members remain
        shareholders and are inside the eligible count.
        Distribution re-verifies the snapshot against live balances and
        conflicts on any drift.
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
          This declaration is {record.status} — a terminal state. No further
          lifecycle action exists for it (the server enforces the transition
          matrix regardless).
        </div>
      )}

      {!terminal && record.status === "declared" && (
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
                    declaration (both server-enforced); the quorum is resolved
                    server-side at vote time.
                  </div>
                  {alreadyVoted && voteResult === null && (
                    <div className={styles.formNote}>
                      This tab already recorded your vote on this declaration —
                      one vote per committee member; the buttons stay spent.
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
                  Your role has no transactions approval permission — dividend
                  voting controls are not offered.
                </div>
              )
            }
          >
            <div className={styles.formNote}>
              Awaiting committee decision on a total payout of{" "}
              {fmtKes(record.total_payout)} (server snapshot).
            </div>
          </MakerCheckerPanel>
        </>
      )}

      {!terminal && record.status === "approved" && (
        <div className={styles.formNote}>
          The committee approved this snapshot. Running the distribution moves
          money for every eligible member — the server re-verifies the
          snapshot against live balances first and posts NOTHING on drift
          (the remedy is voiding and re-declaring).
        </div>
      )}

      <div className={styles.actions}>
        {/* VOID: offered on any OPEN declaration to transactions:approve
            holders ONLY — the contract gate on POST.../void is
            transactions:approve (backend api/dividends.py,
            TxnApproveCtx), and an approve-holding declarer may void
            their OWN declaration (it moves no money and only narrows
            state — the documented drift remedy freeing the FY slot).
            The server refuses the void once any distribution claim
            exists. */}
        {!terminal && mayApprove && (
          <Button
            type="button"
            variant="danger"
            onClick={() => setConfirmVoid(true)}
            disabled={anyPending}
          >
            Void declaration…
          </Button>
        )}
        {!terminal && record.status === "approved" && mayApprove && (
          <Button
            type="button"
            variant="primary"
            onClick={() => onDistribute(record)}
            disabled={anyPending}
          >
            Run distribution…
          </Button>
        )}
      </div>

      {confirmVote !== null && (
        <ConfirmDangerModal
          title={confirmVote === "approve" ? "Cast approve vote" : "Cast reject vote"}
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel={confirmVote === "approve" ? "Confirm approve vote" : "Confirm reject vote"}
          pending={vote.isPending}
          onConfirm={() => {
            if (!vote.isPending) vote.mutate(confirmVote);
          }}
          onClose={() => setConfirmVote(null)}
        >
          <Banner>
            Your vote is recorded once and cannot be changed (one vote per
            committee member). A quorum of votes decides the declaration —
            approval authorises distributing a total payout of{" "}
            {fmtKes(record.total_payout)} (server snapshot).
          </Banner>
        </ConfirmDangerModal>
      )}

      {confirmVoid && (
        <ConfirmDangerModal
          title="Void dividend declaration"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Void declaration"
          pending={voidMutation.isPending}
          onConfirm={() => {
            if (!voidMutation.isPending) voidMutation.mutate(record.version);
          }}
          onClose={() => setConfirmVoid(false)}
        >
          <Banner>
            This voids the open declaration (state narrows to rejected; no
            money moves) and frees the one-live-declaration-per-financial-year
            slot so a fresh declaration captures current figures. The server
            refuses once any distribution claim exists — a partially
            distributed year can never be re-declared. The write pins record
            version {record.version} — any drift is a conflict with nothing
            changed.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
