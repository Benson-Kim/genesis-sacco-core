"use client";

/**
 * Application detail drawer (module 3 — prototype `vAppDetail`):
 * stage workflow indicator, applicant card (resolved via the members
 * module — the P9 record carries member_id only), server-computed
 * figures rendered verbatim, and the officer stage moves.
 *
 * - Transitions send the `version` the record was loaded with; a 409
 *   renders the shared ConflictBanner's explicit reload-and-re-enter
 *   flow — never a silent overwrite, never an auto-retry, exactly one
 *   write attempt per intent (mutations retry: 0).
 * - REJECT is terminal (it releases live guarantor pledges server-side)
 *   and therefore goes through ConfirmDangerModal's typed confirmation.
 * - APPROVED is not an officer move: only committee quorum produces it
 *   (and only the loan-book disbursement contract produces DISBURSED) —
 *   the UI never offers what the API forbids (least disclosure).
 * - When THIS operator recommends to committee, the per-tab maker
 *   registry records them so the committee screen's MakerCheckerPanel
 *   structurally withholds their own vote affordances.
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchMember } from "@/modules/members/api";
import { fetchApplication, transitionApplication, type TransitionTarget } from "../api";
import { recordCommitteeMaker } from "../makerRegistry";
import {
  APPLICATION_STAGES,
  STAGE_LABELS,
  type Application,
  type Product,
} from "../schemas";
import { coverPill, stagePill } from "./pills";
import styles from "./Applications.module.css";

/** The linear workflow (prototype indicator); rejected renders as a banner. */
const WORKFLOW = APPLICATION_STAGES.filter((stage) => stage !== "rejected");

function StageIndicator({ stage }: Readonly<{ stage: Application["stage"] }>) {
  if (stage === "rejected") {
    return <Banner variant="error">Application rejected — this stage is terminal.</Banner>;
  }
  const index = WORKFLOW.indexOf(stage);
  return (
    <ol className={styles.stages} aria-label="Workflow stage">
      {WORKFLOW.map((step, i) => {
        const cls =
          i < index
            ? `${styles.stageStep} ${styles.stageStepDone}`
            : i === index
              ? `${styles.stageStep} ${styles.stageStepCurrent}`
              : styles.stageStep;
        return (
          <li key={step} className={cls} aria-current={i === index ? "step" : undefined}>
            {i < index ? "✓ " : ""}
            {STAGE_LABELS[step]}
          </li>
        );
      })}
    </ol>
  );
}

export function ApplicationDetailDrawer({
  applicationId,
  products,
  onClose,
}: Readonly<{
  applicationId: string;
  products: Product[];
  onClose: () => void;
}>) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const [confirmReject, setConfirmReject] = useState(false);
  // Notices carry a TONE (W59-4): successes are "ok"; the post-conflict
  // reload notice is informational — never success-styled.
  const [notice, setNotice] = useState<{ text: string; tone: "ok" | "info" } | null>(null);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const detail = useQuery({
    queryKey: ["applications", "detail", applicationId],
    queryFn: () => fetchApplication(applicationId),
    // Record class: NEVER a stale record into an optimistic-locked move.
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

  const transition = useMutation({
    mutationFn: (input: { target: TransitionTarget; version: number }) =>
      transitionApplication(
        applicationId,
        input,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({ op: "transition", id: applicationId, input }),
        ),
      ),
    onSuccess: (updated, input) => {
      queryClient.setQueryData(["applications", "detail", applicationId], updated);
      void queryClient.invalidateQueries({ queryKey: ["applications", "list"] });
      if (input.target === "committee") {
        // Separation of duties (per-tab witness): THIS operator is the
        // maker of the committee referral — the committee screen's
        // MakerCheckerPanel withholds their own vote affordances.
        const ownId = getOwnUserId();
        if (ownId !== null) recordCommitteeMaker(applicationId, ownId);
      }
      setConfirmReject(false);
      setNotice({
        text:
          input.target === "rejected"
            ? "Application rejected."
            : input.target === "committee"
              ? "Recommended to committee."
              : "Moved to appraisal.",
        tone: "ok",
      });
      announce("Stage updated.");
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banner below reports the (single) failed attempt.
      setConfirmReject(false);
    },
  });

  if (detail.isPending) {
    return (
      <Modal title="Application detail" onClose={onClose}>
        <div className={styles.muted}>Loading…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Application detail" onClose={onClose}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const app = detail.data;
  const mayEdit = can(permissions.data, "applications", "edit");
  const product = products.find((p) => p.id === app.product_id);
  const conflict = transition.isError && isConflict(transition.error);

  function reloadRecord() {
    // Explicit reload flow: refetch the current record; the stale move is
    // NEVER replayed (a re-attempt is a NEW operator intent with a new
    // version in the body, hence a rotated Idempotency-Key).
    void queryClient.refetchQueries({ queryKey: ["applications", "detail", applicationId] });
    transition.reset();
    setNotice({ text: "Record reloaded — re-check the stage before acting again.", tone: "info" });
    // Every async outcome is announced: the post-conflict
    // reload is an outcome, not a success (W59-4, the F5 class).
    announce("Record reloaded after the conflict — re-check the stage.");
  }

  const move = (target: TransitionTarget) => {
    if (transition.isPending) return;
    transition.mutate({ target, version: app.version });
  };

  return (
    <Modal title="Application detail" onClose={onClose} closeDisabled={transition.isPending}>
      {/* Success notices keep the ok styling; the post-conflict reload
          notice is informational (W59-4, the F5 class). */}
      {notice !== null && <Banner variant={notice.tone}>{notice.text}</Banner>}
      <StageIndicator stage={app.stage} />
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
        <Kv label="Amount">{fmtKes(app.amount)}</Kv>
        <Kv label="Term">{app.term_months} months</Kv>
        <Kv label="Rate">{app.rate_pct}% (product-set)</Kv>
        <Kv label="Security cover">{coverPill(app.cover_pct)}</Kv>
        <Kv label="Max eligible">
          {app.max_eligible !== null ? fmtKes(app.max_eligible) : "—"}
        </Kv>
        <Kv label="Purpose">{app.purpose ?? "—"}</Kv>
        <Kv label="Stage">{stagePill(app.stage)}</Kv>
        <Kv label="Created by">
          {/* Initiator attribution (/ follow-up): the
              SERVER's bare staff UUID, short-id convention — least
              disclosure: no name/email is ever fetched for it.
              NULL renders the honest unattributed affordance:
              an actor is never invented. */}
          {app.created_by !== null ? (
            <span className={styles.mono} title={app.created_by}>
              {app.created_by.slice(0, 8)}
            </span>
          ) : (
            "— (unattributed)"
          )}
        </Kv>
        <Kv label="Recommended by">
          {/* Recommender attribution (close-out, 0037): the
              SERVER's bare staff UUID, short-id convention — least
              disclosure: no name/email is ever fetched for it.
              NULL renders the honest affordance: not yet
              referred to committee, or unattributed history — an actor
              is never invented. */}
          {app.recommended_by !== null ? (
            <span className={styles.mono} title={app.recommended_by}>
              {app.recommended_by.slice(0, 8)}
            </span>
          ) : (
            "— (not referred / unattributed)"
          )}
        </Kv>
        <Kv label="Record version">{app.version}</Kv>
      </div>
      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={transition.error} onReload={reloadRecord} />
      {transition.isError && !conflict && <ErrorBanner error={transition.error} />}
      {mayEdit && (
        <>
          <div className={styles.subhead}>Officer action</div>
          {app.stage === "submitted" && (
            <div className={styles.actions}>
              <Button variant="danger" onClick={() => setConfirmReject(true)} disabled={transition.isPending}>
                Reject…
              </Button>
              <Button
                variant="primary"
                onClick={() => move("appraisal")}
                disabled={transition.isPending}
              >
                {transition.isPending ? "Working…" : "Move to appraisal"}
              </Button>
            </div>
          )}
          {app.stage === "appraisal" && (
            <div className={styles.actions}>
              <Button variant="danger" onClick={() => setConfirmReject(true)} disabled={transition.isPending}>
                Reject…
              </Button>
              <Button
                variant="primary"
                onClick={() => move("committee")}
                disabled={transition.isPending}
              >
                {transition.isPending ? "Working…" : "Recommend to committee"}
              </Button>
            </div>
          )}
          {app.stage === "committee" && (
            <>
              <div className={styles.formNote}>
                Approval is decided by committee voting (Credit committee screen) —
                there is no direct approve action here.
              </div>
              <div className={styles.actions}>
                <Button variant="danger" onClick={() => setConfirmReject(true)} disabled={transition.isPending}>
                  Reject…
                </Button>
              </div>
            </>
          )}
          {app.stage === "approved" && (
            <div className={styles.formNote}>
              Approved by committee. Disbursement runs through the loan-book module
              (next batch) — it is not offered here.
            </div>
          )}
          {(app.stage === "rejected" || app.stage === "disbursed") && (
            <div className={styles.formNote}>This stage is terminal — no officer action.</div>
          )}
        </>
      )}
      {confirmReject && (
        <ConfirmDangerModal
          title="Reject application"
          confirmPhrase={app.id.slice(0, 8)}
          confirmLabel="Reject application"
          pending={transition.isPending}
          onConfirm={() => move("rejected")}
          onClose={() => setConfirmReject(false)}
        >
          <Banner>
            Rejection is terminal and releases any live guarantor pledges in the
            same server-side transaction. It cannot be undone from this screen.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
