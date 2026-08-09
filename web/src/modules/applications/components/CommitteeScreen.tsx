"use client";

/**
 * Credit-committee review screen (module 3 — prototype `vCommittee`): agenda of committee-stage applications (keyset) plus
 * the review panel, on the SHARED grid.sideMain (no private page grid).
 *
 * MAKER-CHECKER (blocker (f)): every vote affordance mounts INSIDE
 * MakerCheckerPanel — the checker controls exist only for a different,
 * known principal (getOwnUserId; no override prop). The maker identity
 * is SERVER TRUTH first (close-out, 0037): the contract's
 * `recommended_by` (the committee referrer — the vote SoD subject the
 * server enforces) with `created_by` (the initiator, /0036) also
 * withheld structurally; the per-tab registry (makerRegistry.ts) is
 * the fallback witness for unattributed rows only. The server enforces
 * the voting invariants regardless (least disclosure): one vote per voter (DB
 * UNIQUE ⇒ 409), the RECOMMENDER cannot vote (403, keyed on the
 * persisted recommended_by — 0037), voting open only in committee
 * stage, quorum from tenant config AT VOTE TIME, authority bands on
 * approve votes.
 *
 * MONEY (blocker (a)): the amount, rate and cover render as API decimal
 * strings; the tally and decision come from VoteResultOut — nothing is
 * computed client-side (no installment preview: the API exposes none).
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Card, ConfirmDangerModal, Kv, Pill } from "@genesis/design-system";
import { MakerCheckerPanel } from "@/modules/authz/components/MakerCheckerPanel";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { fetchMember } from "@/modules/members/api";
import { fetchApplication, fetchApplicationsPage, voteOnApplication } from "../api";
import { committeeMakerOf, MAKER_UNKNOWN } from "../makerRegistry";
import { recordVotedApplication, useHasVotedOn } from "../votedRegistry";
import { getOwnUserId } from "@/modules/auth/session";
import type { Application, Vote, VoteResult } from "../schemas";
import { useProducts } from "../useProducts";
import { coverPill } from "./pills";
import grid from "@/modules/layout/grid.module.css";
import styles from "./Applications.module.css";

const AGENDA_FILTERS = { stage: "committee" } as const;

export function CommitteeScreen() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const agenda = useKeysetList<Application>({
    queryKey: ["applications", "list", AGENDA_FILTERS],
    fetchPage: (cursor) => fetchApplicationsPage(AGENDA_FILTERS, cursor),
  });

  const items = agenda.data?.pages.flatMap((page) => page.items) ?? [];
  const selected = items.find((app) => app.id === selectedId) ?? items[0];

  if (agenda.isPending) {
    return <div className={styles.muted}>Loading committee agenda…</div>;
  }

  if (agenda.isError) {
    return <ErrorBanner error={agenda.error} />;
  }

  if (items.length === 0 || selected === undefined) {
    return (
      <Card>
        <div className={styles.muted}>No applications at committee stage.</div>
      </Card>
    );
  }

  return (
    <div className={grid.sideMain}>
      <Card padded={false}>
        <div className={styles.agendaHead}>Committee agenda</div>
        <ul className={styles.agendaList}>
          {items.map((app) => (
            <li key={app.id}>
              <button
                type="button"
                className={styles.agendaButton}
                aria-pressed={app.id === selected.id}
                onClick={() => setSelectedId(app.id)}
              >
                <span className={styles.cellStrong}>{fmtKes(app.amount)}</span>
                <span className={styles.cellSub}>
                  {app.purpose ?? "—"} · {app.term_months} mo
                </span>
              </button>
            </li>
          ))}
        </ul>
        {agenda.hasNextPage && (
          <div className={styles.agendaMore}>
            <Button
              onClick={() => void agenda.fetchNextPage()}
              disabled={agenda.isFetchingNextPage}
            >
              {agenda.isFetchingNextPage ? "Loading…" : "Load more"}
            </Button>
          </div>
        )}
      </Card>
      <ReviewPanel key={selected.id} applicationId={selected.id} />
    </div>
  );
}

function ReviewPanel({ applicationId }: Readonly<{ applicationId: string }>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const products = useProducts();
  const [confirmVote, setConfirmVote] = useState<Vote | null>(null);
  const [result, setResult] = useState<VoteResult | null>(null);
  // Spent affordance across remounts (W58-6): switching agenda items and
  // back must not re-arm the vote buttons after a recorded vote.
  const alreadyVoted = useHasVotedOn(applicationId);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const detail = useQuery({
    queryKey: ["applications", "detail", applicationId],
    queryFn: () => fetchApplication(applicationId),
    // Record class: the vote decision context must be the freshest read.
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
    mutationFn: (choice: Vote) =>
      voteOnApplication(
        applicationId,
        choice,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({ op: "vote", id: applicationId, choice }),
        ),
      ),
    onSuccess: (tally) => {
      setConfirmVote(null);
      setResult(tally);
      // The vote is SPENT for this tab (W58-6): the per-tab registry
      // keeps the affordance withdrawn across panel remounts.
      recordVotedApplication(applicationId);
      announce(tally.decision === null ? "Vote recorded." : "Committee decision reached.");
      // The vote may have decided the application: refresh the record and
      // every list (the agenda drops decided applications).
      void queryClient.invalidateQueries({ queryKey: ["applications", "detail", applicationId] });
      void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
    },
    onError: () => {
      setConfirmVote(null);
    },
  });

  if (detail.isPending) {
    return (
      <Card>
        <div className={styles.muted}>Loading application…</div>
      </Card>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Card>
        <ErrorBanner error={detail.error} />
      </Card>
    );
  }

  const app = detail.data;
  const mayApprove = can(permissions.data, "applications", "approve");
  const product = products.data?.find((p) => p.id === app.product_id);
  const conflict = vote.isError && isConflict(vote.error);
  // SERVER TRUTH FIRST (close-out, 0037): recommended_by is
  // the contract's recommender attribution — the vote's SoD subject
  // (the server refuses the recommender's vote, 403). created_by (the
  // initiator, /0036) stays structurally withheld too — EXTEND,
  // never weaken: if the signed-in operator is EITHER server-attributed
  // principal, the panel's self-check matches and the vote affordances
  // are withheld. The registry (the referral THIS TAB witnessed) is
  // consulted only when the server record carries NO attribution at
  // all; nothing is ever invented.
  const witnessedMakerId = committeeMakerOf(app.id);
  const ownId = getOwnUserId();
  const serverMakerId = app.recommended_by ?? app.created_by;
  const makerId =
    ownId !== null && (ownId === app.recommended_by || ownId === app.created_by)
      ? ownId
      : (serverMakerId ?? witnessedMakerId);
  const makerLabel =
    app.recommended_by !== null
      ? app.recommended_by === ownId
        ? "You recommended this application to committee (server attribution) — the server refuses your vote."
        : // Least disclosure: the bare staff UUID via the
          // short-id convention — never a name or email.
          `Recommending officer ${app.recommended_by.slice(0, 8)} (server attribution).`
      : app.created_by !== null
        ? app.created_by === ownId
          ? "You initiated this application (server attribution)."
          : `Initiating officer ${app.created_by.slice(0, 8)} (server attribution).`
        : witnessedMakerId === MAKER_UNKNOWN
          ? "Referring officer — unattributed on the server record (attribution is never invented); no referral witnessed by this tab."
          : witnessedMakerId === ownId
            ? "You recommended this application to committee (witnessed by this tab)."
            : "Recommending officer (witnessed by this tab).";

  function reloadRecord() {
    // Explicit reload flow (409: already voted / stage moved underneath):
    // refetch the record and agenda — the vote is NEVER replayed.
    void queryClient.refetchQueries({ queryKey: ["applications", "detail", applicationId] });
    void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
    vote.reset();
  }

  return (
    <Card>
      <div className={styles.reviewHead}>
        <span className={styles.reviewTitle}>
          Loan review —{" "}
          {member.data !== undefined ? (
            `${member.data.name} · ${member.data.member_no}`
          ) : (
            <span className={styles.mono}>{app.member_id}</span>
          )}
        </span>
        <Pill bg="goldSoft" color="navy">
          Committee stage
        </Pill>
      </div>
      <div className={styles.statRow}>
        <div className={styles.statBlock}>
          <div className={styles.statLabel}>Amount</div>
          <div className={styles.statValue}>{fmtKes(app.amount)}</div>
        </div>
        <div className={styles.statBlock}>
          <div className={styles.statLabel}>Rate</div>
          <div className={styles.statValue}>{app.rate_pct}%</div>
        </div>
        <div className={styles.statBlock}>
          <div className={styles.statLabel}>Term</div>
          <div className={styles.statValue}>{app.term_months} mo</div>
        </div>
        <div className={styles.statBlock}>
          <div className={styles.statLabel}>Cover</div>
          <div className={styles.statValue}>{coverPill(app.cover_pct)}</div>
        </div>
      </div>
      {/* Design-system Kv rows — default
          variant, byte-equivalent rules to the module CSS this replaced;
          the product-id fallback keeps its nested mono span in children
          (the ApplicationDetailDrawer precedent — identical DOM). */}
      <Kv label="Product">
        {product !== undefined ? product.name : <span className={styles.mono}>{app.product_id}</span>}
      </Kv>
      <Kv label="Purpose">{app.purpose ?? "—"}</Kv>
      {result !== null && (
        <div className={styles.tally} role="status">
          <Pill bg="emeraldSoft" color="emerald">
            {result.approvals} approve
          </Pill>
          <Pill bg="brickSoft" color="brick">
            {result.rejections} reject
          </Pill>
          {result.decision !== null ? (
            <Pill bg={result.decision === "approved" ? "emeraldSoft" : "brickSoft"} color={result.decision === "approved" ? "emerald" : "brick"}>
              Decision: {result.decision}
            </Pill>
          ) : (
            <Pill>Awaiting quorum</Pill>
          )}
        </div>
      )}
      {/* One copy of the 409 reload flow (reuse-first): an already-voted or
          stage-moved conflict is reloaded, never replayed. */}
      <ConflictBanner error={vote.error} onReload={reloadRecord} />
      {vote.isError && !conflict && <ErrorBanner error={vote.error} />}
      <div className={styles.subhead}>Committee decision</div>
      <MakerCheckerPanel
        makerId={makerId}
        makerLabel={makerLabel}
        checkerActions={
          mayApprove ? (
            <>
              <div className={styles.formNote}>
                One vote per committee member (enforced by the server); the quorum
                is resolved server-side at vote time. Approve votes are capped by
                your approval-authority band.
              </div>
              {alreadyVoted && result === null && (
                <div className={styles.formNote}>
                  This tab already recorded your vote on this application —
                  one vote per committee member; the buttons stay spent.
                </div>
              )}
              <div className={styles.voteActions}>
                <Button
                  variant="danger"
                  onClick={() => setConfirmVote("reject")}
                  disabled={vote.isPending || result !== null || alreadyVoted}
                >
                  Vote reject
                </Button>
                <Button
                  variant="primary"
                  onClick={() => setConfirmVote("approve")}
                  disabled={vote.isPending || result !== null || alreadyVoted}
                >
                  Vote approve
                </Button>
              </div>
            </>
          ) : (
            <div className={styles.formNote}>
              Your role has no committee approval permission — voting controls are
              not offered.
            </div>
          )
        }
      >
        <div className={styles.formNote}>
          Awaiting committee decision on {fmtKes(app.amount)} over {app.term_months} months.
        </div>
      </MakerCheckerPanel>
      {/* Typed confirmation (W58-3, blocker (f)): a vote decides money
          movement (quorum approval leads directly to disbursement), so
          it carries the same ConfirmDangerModal treatment as every
          other money-adjacent write — typed phrase, pending
          short-circuit, exactly one wire write per confirmation. */}
      {confirmVote !== null && (
        <ConfirmDangerModal
          title={confirmVote === "approve" ? "Cast approve vote" : "Cast reject vote"}
          confirmPhrase={app.id.slice(0, 8)}
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
            committee member). A quorum of votes decides the application
            — approval leads directly to disbursement.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Card>
  );
}
