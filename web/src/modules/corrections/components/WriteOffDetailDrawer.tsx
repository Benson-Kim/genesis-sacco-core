"use client";

/**
 * Write-off record drawer (the committee workflow plus the issue- recovery trail): the full write-once snapshot
 * (WriteOffOut) with the lifecycle writes its status allows —
 * committee votes and void (requested), posting (approved), and the
 * recovery receipts trail (posted). Votes/void/posting are
 * corrections:approve; receipts are corrections:create (offered via
 * the RecoveryReceiptDialog opened from here).
 *
 * - FRESH RECORD READ (record class, staleTime 0): every lifecycle
 *   write arms against the freshest available read; the version
 *   anchors the void body and every key material.
 * - MAKER-CHECKER (blocker (f)): vote and posting affordances mount
 *   ONLY inside MakerCheckerPanel. WriteOffOut does NOT serialise
 *   requested_by (0025 has the column; the gap is recorded as a contract follow-up on), so the maker identity is the
 *   per-tab REGISTRY witness only — the documented FALLBACK leg
 *   (server truth would supersede it if the field existed): a request
 *   witnessed by this tab structurally withholds the checker controls
 *   for its maker; an unwitnessed record renders them with the honest
 *   label, and the server 403 stays the enforcer (least disclosure).
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit, `retry: 0`; vote/post key material folds
 *   the fresh record VERSION + reload epoch (rule 4 — the posting
 *   body is EMPTY by contract, so the KEY pins the acted-on
 *   snapshot); void folds the version it also pins on the wire
 *   (rule 3). Votes are SPENT across remounts (votedRegistry, W58-6).
 * - A 409 (snapshot drift at posting — the server re-verifies
 *   component-by-component and posts NOTHING; or a vote/void raced)
 *   renders the shared ConflictBanner's explicit reload-and-re-enter
 *   flow with the remedy stated: void the drifted
 *   snapshot and raise a fresh request. NOTHING is replayed.
 * - MONEY (blocker (a)): every figure (snapshot components, posting
 *   result, receipts, recovered/outstanding position) is the SERVER's
 *   decimal string rendered verbatim via fmtKes — never summed,
 *   netted or re-derived (the receipts table has NO totals row; the
 *   position lines come from the receipts response itself).
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
import { useKeysetList } from "@/modules/table/useKeysetList";
import { loanClassPill } from "@/modules/loans/components/pills";
import {
  fetchRecoveryReceiptsPage,
  fetchWriteOff,
  postWriteOff,
  voidWriteOff,
  voteOnWriteOff,
} from "../api";
import { WRITE_OFF_MAKER_UNKNOWN, writeOffMakerOf } from "../makerRegistry";
import { recordVotedWriteOff, useHasVotedOnWriteOff } from "../votedRegistry";
import {
  isTerminalWriteOffStatus,
  type RecoveryReceiptsPage,
  type WriteOffPostResult,
  type WriteOffVote,
  type WriteOffVoteResult,
} from "../schemas";
import { writeOffStatusPill } from "./pills";
import styles from "./Corrections.module.css";

/** Bare staff UUID under least disclosure (the short-id convention). */
function ShortId({ id }: Readonly<{ id: string }>) {
  return (
    <span className={styles.mono} title={id}>
      {id.slice(0, 8)}
    </span>
  );
}

export function WriteOffDetailDrawer({
  writeOffId,
  onRecordReceipt,
  onClose,
}: Readonly<{
  writeOffId: string;
  /** Open the recovery-receipt dialog for this POSTED record. */
  onRecordReceipt: () => void;
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [confirmVote, setConfirmVote] = useState<WriteOffVote | null>(null);
  const [confirmVoid, setConfirmVoid] = useState(false);
  const [confirmPost, setConfirmPost] = useState(false);
  const [voteResult, setVoteResult] = useState<WriteOffVoteResult | null>(null);
  const [postResult, setPostResult] = useState<WriteOffPostResult | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency keys.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const voteKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const voidKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  const postKeySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });
  // Spent affordance across remounts (W58-6).
  const alreadyVoted = useHasVotedOnWriteOff(writeOffId);

  const detail = useQuery({
    queryKey: ["corrections", "write-off", writeOffId],
    queryFn: () => fetchWriteOff(writeOffId),
    // Record class: NEVER a stale record into a lifecycle write.
    staleTime: STALE_TIME.record,
  });

  const vote = useMutation({
    // STRUCTURAL freshness guard: the vote never fires without
    // the loaded fresh record.
    mutationFn: (choice: WriteOffVote) => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh write-off record is not loaded — the vote was NOT recorded."),
        );
      }
      return voteOnWriteOff(
        writeOffId,
        choice,
        idempotencyKeyFor(
          voteKeySlot.current,
          JSON.stringify({
            op: "write-off-vote",
            id: writeOffId,
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
      // The vote is SPENT for this tab (W58-6).
      recordVotedWriteOff(writeOffId);
      setNotice("");
      announce(
        tally.decision === null
          ? "Write-off vote recorded — awaiting quorum."
          : "Committee decision reached on the write-off.",
      );
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-off", writeOffId] });
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-offs-register"] });
    },
    onError: () => {
        setConfirmVote(null);
      announce("The write-off vote was NOT recorded.");
    },
  });

  const voidMutation = useMutation({
    mutationFn: () => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh write-off record is not loaded — nothing was voided."),
        );
      }
      return voidWriteOff(
        writeOffId,
        fresh.version,
        idempotencyKeyFor(
          voidKeySlot.current,
          // Versioned-write material (README rule 3): the version is
          // ALSO pinned in the wire body.
          JSON.stringify({
            op: "write-off-void",
            id: writeOffId,
            version: fresh.version,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: () => {
      setConfirmVoid(false);
      setNotice("");
      announce("Write-off voided — the loan's write-off slot is free again.");
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-off", writeOffId] });
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-offs-register"] });
    },
    onError: () => {
      setConfirmVoid(false);
      announce("The write-off was NOT voided.");
    },
  });

  const post = useMutation({
    // STRUCTURAL freshness guard: the TERMINAL money write
    // refuses to fire without the loaded fresh record.
    mutationFn: () => {
      const fresh = detail.data;
      if (fresh === undefined) {
        return Promise.reject(
          new Error("The fresh write-off record is not loaded — nothing was posted."),
        );
      }
      return postWriteOff(
        writeOffId,
        idempotencyKeyFor(
          postKeySlot.current,
          // Money-mover material (README rule 4): the wire body is
          // DELIBERATELY EMPTY, so the KEY pins the
          // acted-on snapshot — fresh record version + reload epoch.
          // Posting is once-per-record, so no per-success counter.
          JSON.stringify({
            op: "write-off-post",
            id: writeOffId,
            recordVersion: fresh.version,
            reload_epoch: reloadEpoch,
          }),
        ),
      );
    },
    onSuccess: (posted) => {
      setConfirmPost(false);
      // SPENT affordance: the result panel replaces the action.
      setPostResult(posted);
      setNotice("");
      announce("Write-off posted — the receivable is derecognised; the claim survives.");
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-off", writeOffId] });
      void queryClient.invalidateQueries({ queryKey: ["corrections", "write-offs-register"] });
      void queryClient.invalidateQueries({ queryKey: ["transactions", "list"] });
    },
    onError: () => {
      setConfirmPost(false);
      announce("The write-off was NOT posted.");
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Loan write-off" onClose={onClose} dismissOnOverlay={false}>
        <div className={styles.muted}>Loading write-off record…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Loan write-off" onClose={onClose} dismissOnOverlay={false}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;
  const mayApprove = can(permissions.data, "corrections", "approve");
  const mayCreate = can(permissions.data, "corrections", "create");
  const terminal = isTerminalWriteOffStatus(record.status);
  const anyPending = vote.isPending || voidMutation.isPending || post.isPending;
  const voteConflict = vote.isError && isConflict(vote.error);
  const voidConflict = voidMutation.isError && isConflict(voidMutation.error);
  const postConflict = post.isError && isConflict(post.error);

  // FALLBACK-ONLY attribution (the pattern, honestly): the
  // contract does not serialise requested_by, so the per-tab witness
  // is the only maker signal — a request witnessed by this tab
  // withholds the checker controls for its maker; everything else is
  // honestly UNKNOWN (the sentinel never equals a real user id).
  const witnessedMakerId = writeOffMakerOf(record.id);
  const ownId = getOwnUserId();
  const makerLabel =
    witnessedMakerId === WRITE_OFF_MAKER_UNKNOWN
      ? "Requesting officer — not on the record (the contract does not attribute the requester); no request witnessed by this tab. The server still bans the requester from voting or posting."
      : witnessedMakerId === ownId
        ? "You requested this write-off (witnessed by this tab)."
        : "Requesting officer (witnessed by this tab).";

  function reloadRecord() {
    // Explicit reload flow: refetch the record; the stale
    // write is NEVER replayed — re-entering it is a NEW intent whose
    // key rotates via the reload epoch.
    void queryClient.refetchQueries({ queryKey: ["corrections", "write-off", writeOffId] });
    setReloadEpoch((epoch) => epoch + 1);
    vote.reset();
    voidMutation.reset();
    post.reset();
    setNotice(
      "Record reloaded — nothing was posted. If the loan drifted since the snapshot, void this write-off and raise a fresh request to capture current balances.",
    );
    announce("Record reloaded after the conflict — nothing was posted.");
  }

  return (
    <Modal
      title="Loan write-off"
      onClose={onClose}
      closeDisabled={anyPending}
      // W56-3: this drawer bears money-write affordances.
      dismissOnOverlay={false}
    >
      {notice !== "" && <Banner>{notice}</Banner>}

      <div className={styles.detailGrid}>
        <Kv label="Status">{writeOffStatusPill(record.status)}</Kv>
        <Kv label="Write-off id">
          <span className={styles.mono}>{record.id}</span>
        </Kv>
        <Kv label="Loan">
          <ShortId id={record.loan_id} />
        </Kv>
        <Kv label="Member">
          <ShortId id={record.member_id} />
        </Kv>
        <Kv label="Classification">{loanClassPill(record.classification)}</Kv>
        <Kv label="Provision">{record.provision_pct}%</Kv>
        <Kv label="Reason">{record.reason}</Kv>
        <Kv label="Requested">{fmtDateTime(record.created_at)}</Kv>
        <Kv label="Decided">{fmtDateTime(record.decided_at)}</Kv>
        <Kv label="Posted">{fmtDateTime(record.posted_at)}</Kv>
        {record.transaction_id !== null && (
          <Kv label="WO- ledger row">
            <span className={styles.mono}>{record.transaction_id}</span>
          </Kv>
        )}
        <Kv label="Record version">{record.version}</Kv>
      </div>

      <div className={styles.subhead}>Write-once snapshot (server-computed)</div>
      <div className={styles.detailGrid}>
        <Kv label="Balance (receivable)">
          <span className={styles.amountCell}>{fmtKes(record.balance)}</span>
        </Kv>
        <Kv label="Penalty due">
          <span className={styles.amountCell}>{fmtKes(record.penalty_due)}</span>
        </Kv>
        <Kv label="Total written off (surviving claim)">
          <span className={styles.netCell}>{fmtKes(record.total_written_off)}</span>
        </Kv>
      </div>
      <div className={styles.formNote}>
        Every figure above is the SERVER&apos;s write-once snapshot — nothing
        is summed or re-verified in this screen. Posting re-verifies the
        snapshot component-by-component under the loan row lock and returns a
        conflict on any drift, posting nothing. Write-off is NOT forgiveness:
        the claim survives for recovery, guarantors stay bound and the
        member&apos;s exit is blocked until the claim clears.
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

      {/* One copy of the 409 reload flow (reuse-first). */}
      <ConflictBanner error={vote.error} onReload={reloadRecord} />
      {vote.isError && !voteConflict && <ErrorBanner error={vote.error} />}
      <ConflictBanner error={voidMutation.error} onReload={reloadRecord} />
      {voidMutation.isError && !voidConflict && <ErrorBanner error={voidMutation.error} />}
      <ConflictBanner error={post.error} onReload={reloadRecord} />
      {post.isError && !postConflict && <ErrorBanner error={post.error} />}

      {postResult !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>
            Write-off posted{postResult.txn_ref !== null ? ` · ref ${postResult.txn_ref}` : ""}
          </div>
          <Kv label="Status">{writeOffStatusPill(postResult.status)}</Kv>
          <Kv label="Total written off">
            <span className={styles.netCell}>{fmtKes(postResult.total_written_off)}</span>
          </Kv>
          {postResult.txn_id !== null && (
            <Kv label="WO- ledger row">
              <span className={styles.mono}>{postResult.txn_id}</span>
            </Kv>
          )}
          <div className={styles.formNote}>
            The receivable is derecognised; the legal claim survives on the
            write-once snapshot. Recovery receipts are the ONLY money-in path
            against it — record them from this drawer as cash returns.
          </div>
        </div>
      )}

      {terminal && record.status === "rejected" && (
        <div className={styles.formNote}>
          This write-off is rejected — a terminal state; the loan&apos;s
          write-off slot is free for a fresh request (the server enforces the
          transition matrix regardless).
        </div>
      )}

      {!terminal && record.status === "requested" && (
        <>
          <div className={styles.subhead}>Committee decision</div>
          <MakerCheckerPanel
            makerId={witnessedMakerId}
            makerLabel={makerLabel}
            makerAt={fmtDateTime(record.created_at)}
            checkerActions={
              mayApprove ? (
                <>
                  <div className={styles.formNote}>
                    One vote per committee member and no vote on your own
                    request (both server-enforced); the quorum is resolved
                    server-side at vote time. Assurance roles are never
                    checkers.
                  </div>
                  {alreadyVoted && voteResult === null && (
                    <div className={styles.formNote}>
                      This tab already recorded your vote on this write-off —
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
                  Your role has no corrections approval permission — committee
                  voting controls are not offered.
                </div>
              )
            }
          >
            <div className={styles.formNote}>
              Awaiting committee decision on a write-off of{" "}
              {fmtKes(record.total_written_off)} (server snapshot).
            </div>
          </MakerCheckerPanel>
        </>
      )}

      {!terminal && record.status === "approved" && postResult === null && (
        <>
          <div className={styles.subhead}>Execute write-off</div>
          <MakerCheckerPanel
            makerId={witnessedMakerId}
            makerLabel={makerLabel}
            makerAt={fmtDateTime(record.created_at)}
            checkerActions={
              mayApprove ? (
                <div className={styles.voteActions}>
                  <Button
                    variant="primary"
                    onClick={() => {
                      if (!post.isPending && detail.data !== undefined) setConfirmPost(true);
                    }}
                    disabled={anyPending}
                  >
                    Post write-off…
                  </Button>
                </div>
              ) : (
                <div className={styles.formNote}>
                  Your role has no corrections approval permission — posting is
                  not offered.
                </div>
              )
            }
          >
            <div className={styles.formNote}>
              Committee-approved write-off awaiting the WO- posting: the
              server re-verifies the snapshot component-by-component at
              execution.
            </div>
          </MakerCheckerPanel>
        </>
      )}

      {/* Mounted ONLY for a posted record: the receipts read never even
          fires for a requested/approved/rejected write-off. */}
      {record.status === "posted" && <ReceiptsTrail writeOffId={writeOffId} />}

      <div className={styles.actions}>
        {!terminal && mayApprove && (
          <Button
            type="button"
            variant="danger"
            onClick={() => {
              if (!voidMutation.isPending && detail.data !== undefined) setConfirmVoid(true);
            }}
            disabled={anyPending}
          >
            Void write-off…
          </Button>
        )}
        {record.status === "posted" && mayCreate && (
          <Button type="button" variant="primary" onClick={onRecordReceipt} disabled={anyPending}>
            Record recovery receipt…
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
            committee member). A quorum decides the write-off — approval
            authorises derecognising {fmtKes(record.total_written_off)} (server
            snapshot); the claim survives for recovery.
          </Banner>
        </ConfirmDangerModal>
      )}

      {confirmVoid && (
        <ConfirmDangerModal
          title="Void write-off"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Void write-off"
          pending={voidMutation.isPending}
          onConfirm={() => {
            if (!voidMutation.isPending) voidMutation.mutate();
          }}
          onClose={() => setConfirmVoid(false)}
        >
          <Banner>
            This voids the write-off workflow (state narrows to rejected; no
            money moves) and frees the loan&apos;s one-live-write-off slot so
            a fresh request can capture current balances. The write pins
            record version {record.version} — any drift is a conflict with
            nothing changed.
          </Banner>
        </ConfirmDangerModal>
      )}

      {confirmPost && (
        <ConfirmDangerModal
          title="Post write-off"
          confirmPhrase={record.id.slice(0, 8)}
          confirmLabel="Post write-off"
          pending={post.isPending}
          onConfirm={() => {
            if (!post.isPending) post.mutate();
          }}
          onClose={() => setConfirmPost(false)}
        >
          <Banner>
            This TERMINALLY writes the loan off: the server posts the WO-
            provisioning entry for {fmtKes(record.total_written_off)} (server
            snapshot, re-verified component-by-component) and the loan can
            never take another repayment — only recovery receipts against the
            surviving claim. The wire body is empty by contract: every figure
            comes from the write-once snapshot.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}

/**
 * The issue- recovery trail for a POSTED write-off: keyset receipts
 * (oldest first, scalability) with the SERVER-reconstructed
 * recovered/outstanding position. Mounted only when the record is
 * posted, so the read never fires for other statuses. The position
 * lines come from the latest page's response figures — the table is
 * NEVER totalled client-side (blocker (a)).
 */
function ReceiptsTrail({ writeOffId }: Readonly<{ writeOffId: string }>) {
  const [position, setPosition] = useState<{
    recovered_total: string;
    outstanding_claim: string;
  } | null>(null);

  const receipts = useKeysetList<RecoveryReceiptsPage["items"][number]>({
    queryKey: ["corrections", "write-off", writeOffId, "receipts"],
    fetchPage: async (cursor) => {
      const page = await fetchRecoveryReceiptsPage(writeOffId, cursor);
      // Server position figures ride every page — stash the freshest.
      setPosition({
        recovered_total: page.recovered_total,
        outstanding_claim: page.outstanding_claim,
      });
      return { items: page.items, nextCursor: page.next_cursor };
    },
  });
  const receiptRows = receipts.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <>
      <div className={styles.subhead}>Recovery trail (the surviving claim)</div>
      {position !== null && (
        <div className={styles.detailGrid}>
          <Kv label="Recovered so far">
            <span className={styles.amountCell}>{fmtKes(position.recovered_total)}</span>
          </Kv>
          <Kv label="Outstanding claim">
            <span className={styles.netCell}>{fmtKes(position.outstanding_claim)}</span>
          </Kv>
        </div>
      )}
      {receipts.isError && <ErrorBanner error={receipts.error} />}
      {receiptRows.length === 0 && !receipts.isPending && !receipts.isError && (
        <div className={styles.formNote}>No recovery receipts recorded yet.</div>
      )}
      {receiptRows.length > 0 && (
        <table className={styles.receiptsTable}>
          <thead>
            <tr>
              <th scope="col">Recorded</th>
              <th scope="col">Recorded by</th>
              <th scope="col">Ledger row</th>
              <th scope="col" className="num">
                Amount
              </th>
            </tr>
          </thead>
          <tbody>
            {receiptRows.map((row) => (
              <tr key={row.id}>
                <td>{fmtDateTime(row.created_at)}</td>
                <td>
                  {/* Contract attribution: bare staff UUID, short-id
                      convention (least disclosure). */}
                  <ShortId id={row.recorded_by} />
                </td>
                <td>
                  <ShortId id={row.txn_id} />
                </td>
                <td className="num">
                  <span className={styles.amountCell}>{fmtKes(row.amount)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {receipts.hasNextPage && (
        <div className={styles.actions}>
          <Button
            type="button"
            onClick={() => void receipts.fetchNextPage()}
            disabled={receipts.isFetchingNextPage}
          >
            {receipts.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
      <div className={styles.formNote}>
        The recovered/outstanding position above comes from the receipts
        response — the table is never totalled in this screen. A receipt that
        clears the claim discharges the guarantors and unblocks the
        member&apos;s exit, server-side.
      </div>
    </>
  );
}
