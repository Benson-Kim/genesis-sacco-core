"use client";

/**
 * Declare-dividend drawer (the intake):
 * `POST /dividends/declarations` (transactions:edit) computes and
 * persists the committee-approval snapshot for the LAST COMPLETED
 * financial year, entirely server-side.
 *
 * - NO field exists in this form AT ALL: rates and the
 *   FY period come exclusively from tenant configuration and every
 *   total is computed by the server (`extra="forbid"` server-side —
 *   a caller-supplied rate/period/total is a 422). The body carries
 *   only the server's own documented batch-size default (api.ts
 *   SERVER_DEFAULT_BATCH_SIZE — a tuning, never an operator choice);
 *   the operator's only input is the typed confirmation.
 * - EXACTLY ONE write per intent: ConfirmDangerModal typed phrase,
 *   pending short-circuit + disabled controls, `retry: 0`, one
 *   Idempotency-Key per logical intent. Key material folds a post-409
 *   reload epoch (F3/W59-2) and a per-success intent counter
 *   (W59-3/T2): stable across pure retries of one failed attempt,
 *   rotated when an acknowledged conflict OR a recorded success makes
 *   the next submission a NEW intent. (There is no record version to
 *   fold — declare creates the record; the server's one-live-
 *   declaration-per-FY UNIQUE is the first-write claim.)
 * - A 409 (dividends unconfigured for the tenant, a live declaration
 *   already exists for the FY, or no member has a positive
 *   entitlement) renders the shared ConflictBanner's explicit
 *   reload-and-re-enter flow with an INFORMATIONAL notice +
 *   announce(); the failed request is NEVER replayed.
 * - The result panel renders the SERVER's snapshot VERBATIM (blocker
 *   (a)). The maker identity this tab witnessed is still recorded,
 *   but only as the FALLBACK for unattributed rows — DeclarationOut
 *   now exposes requested_by and server
 *   truth supersedes the per-tab witness; see makerRegistry.
 */
import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, ConfirmDangerModal, Kv, Modal } from "@genesis/design-system";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtKes } from "@/lib/format";
import { declareDividend, SERVER_DEFAULT_BATCH_SIZE } from "../api";
import { recordDividendMaker } from "../makerRegistry";
import type { DeclarationRecord } from "../schemas";
import styles from "./Dividends.module.css";

/** ConfirmDangerModal phrase: no record id exists before the create,
 * so the phrase is the fixed intent word (byte-identical typing is
 * the guard, not the content). */
const DECLARE_PHRASE = "declare";

export function DeclareDividendDrawer({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<DeclarationRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload — a re-entered declaration
  // after a 409 is a NEW intent with a NEW key.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success intent counter (W59-3/T2): a follow-up declaration
  // (e.g. next year, after this one is voided) must never be served
  // the stored response of this one.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const declare = useMutation({
    mutationFn: () =>
      declareDividend(
        idempotencyKeyFor(
          keySlot.current,
          // Key material per the README MATERIAL rule: op + the FULL
          // canonical write body (the server's own batch-size default
          // —: nothing else CAN travel) + reload epoch +
          // per-success intent counter.
          JSON.stringify({
            op: "dividend-declare",
            body: { batch_size: SERVER_DEFAULT_BATCH_SIZE },
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (record) => {
      setConfirming(false);
      // SPENT affordance: the result panel replaces the action.
      setResult(record);
      // The NEXT declaration is a new intent even if byte-identical.
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      // SoD witness (per-tab): the signed-in operator declared this
      // dividend — MakerCheckerPanel withholds their checker controls
      // (the server 403s the self-vote/self-distribute regardless).
      const ownId = getOwnUserId();
      if (ownId !== null) recordDividendMaker(record.id, ownId);
      announce("Dividend declared — awaiting committee decision.");
      void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
    },
    onError: () => {
      // The typed-confirmation dialog must not swallow the failure: it
      // closes and the banners below report the (single) failed attempt.
      setConfirming(false);
      announce("The dividend was NOT declared.");
    },
  });

  const conflict = declare.isError && isConflict(declare.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the register (a live
    // declaration may already exist, or configuration changed); the
    // failed request is structurally WITHDRAWN — re-entering it is a
    // NEW operator intent whose key rotates via the reload epoch.
    void queryClient.invalidateQueries({ queryKey: ["dividends", "list"] });
    declare.reset();
    setReloadEpoch((epoch) => epoch + 1);
    setNotice(
      "Register reloaded — nothing was declared. Check the existing declarations and the tenant's dividend configuration before re-entering.",
    );
    announce("Register reloaded after the conflict — nothing was declared.");
  }

  return (
    <Modal
      title="Declare dividend"
      onClose={onClose}
      closeDisabled={declare.isPending}
      // W56-3: a stray overlay click must never discard the half-armed
      // money-adjacent governance flow.
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling (W59-4, the F5 class). */}
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={declare.error} onReload={reloadAfterConflict} />
      {conflict && (
        <div className={styles.formNote}>
          Typical causes: dividends are not configured for this tenant
          (Settings ▸ Deposits &amp; shares: dividend rate, deposit rebate
          rate, financial-year end month), a live declaration already exists
          for the financial year, or no member has a positive entitlement.
          Nothing was declared.
        </div>
      )}
      {declare.isError && !conflict && <ErrorBanner error={declare.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Dividend declared · awaiting committee decision</div>
          <Kv label="Declaration">
            <span className={styles.mono}>{result.id}</span>
          </Kv>
          <Kv label="Financial year">
            {result.fy_start} → {result.fy_end}
          </Kv>
          <Kv label="Dividend rate (config)">{result.dividend_rate_pct}% p.a.</Kv>
          <Kv label="Rebate rate (config)">{result.rebate_rate_pct}% p.a.</Kv>
          <Kv label="Eligible members (incl. dormant)">{result.eligible_members}</Kv>
          <Kv label="Share basis">{fmtKes(result.total_share_basis)}</Kv>
          <Kv label="Deposit basis">{fmtKes(result.total_deposit_basis)}</Kv>
          <Kv label="Dividend">{fmtKes(result.total_dividend)}</Kv>
          <Kv label="Rebate">{fmtKes(result.total_rebate)}</Kv>
          <Kv label="Total payout">
            <span className={styles.payoutCell}>{fmtKes(result.total_payout)}</span>
          </Kv>
          <div className={styles.formNote}>
            Every figure above is the SERVER&apos;s snapshot for the last
            completed financial year — nothing was computed in this screen.
            The committee approves this exact snapshot; distribution
            re-verifies it against live balances before any money moves.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <>
          <div className={styles.formNote}>
            Declaring computes the payout snapshot for the LAST COMPLETED
            financial year from the tenant&apos;s configured rates — dividend on
            the share basis, rebate on the deposit basis — across every member
            with a positive entitlement (dormant members included). There is
            nothing to enter here: no rate, period or figure
            can travel from this client (the server rejects any such attempt).
            You will not be able to vote on or distribute your own declaration.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={declare.isPending}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                if (!declare.isPending) setConfirming(true);
              }}
              disabled={declare.isPending}
            >
              {declare.isPending ? "Declaring…" : "Declare dividend…"}
            </Button>
          </div>
        </>
      )}

      {confirming && (
        <ConfirmDangerModal
          title="Declare dividend"
          confirmPhrase={DECLARE_PHRASE}
          confirmLabel="Declare dividend"
          pending={declare.isPending}
          onConfirm={() => {
            if (!declare.isPending) declare.mutate();
          }}
          onClose={() => setConfirming(false)}
        >
          <Banner>
            This opens the tenant-wide dividend workflow for the last
            completed financial year: the server snapshots every member&apos;s
            entitlement at the configured rates for committee approval. One
            live declaration per financial year — a concurrent declaration
            collapses to exactly one.
          </Banner>
        </ConfirmDangerModal>
      )}
    </Modal>
  );
}
