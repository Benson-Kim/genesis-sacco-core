"use client";

/**
 * Register-branch drawer: `POST /branches`
 * (settings:create).
 *
 * - The ONLY field is the branch name — exactly the BranchCreateBody
 *   contract (extra="forbid" server-side; the router: no money/cost
 *   parameter exists here for a caller to supply).
 * - EXACTLY ONE write per intent: pending short-circuit + disabled
 *   controls, one Idempotency-Key per logical intent — key material
 *   is the op discriminator + the full canonical body (README
 *   MATERIAL rule 2, the MemberCreateDrawer precedent: a simple
 *   create with the server holding the uniqueness claim).
 * - A 409 (duplicate name after the server's whitespace normalisation
 *   — ' HQ ' can never coexist with 'HQ') renders the shared
 *   ConflictBanner's explicit reload-and-re-enter flow: reload
 *   refetches the register; NOTHING is replayed and re-entering a
 *   corrected name is a NEW intent (changed material rotates the key;
 *   the reload epoch covers the byte-identical re-entry).
 * - The result panel renders the SERVER's record VERBATIM (its
 *   normalised name, not the form's echo).
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { fmtDateTime } from "@/lib/format";
import { createBranch } from "../api";
import { branchNameEntrySchema, type BranchRecord } from "../schemas";
import styles from "./Branches.module.css";

export function BranchCreateDrawer({ onClose }: Readonly<{ onClose: () => void }>) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [result, setResult] = useState<BranchRecord | null>(null);
  const [notice, setNotice] = useState<string>("");
  // Freshness component for the idempotency key: bumped on
  // every explicit post-conflict reload.
  const [reloadEpoch, setReloadEpoch] = useState(0);
  // Per-success intent counter (W59-3): "Register another" followed by
  // an identical entry is a NEW intent with a NEW key.
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const create = useMutation({
    mutationFn: (branchName: string) =>
      createBranch(
        branchName,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "branch-create",
            body: { name: branchName },
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      ),
    onSuccess: (record) => {
      // SPENT affordance: the result panel replaces the form.
      setResult(record);
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce("Branch registered.");
      void queryClient.invalidateQueries({ queryKey: ["branches", "list"] });
    },
    onError: () => {
      announce("The branch was NOT registered.");
    },
  });

  const conflict = create.isError && isConflict(create.error);
  const spent = result !== null;

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the register (the name is
    // probably already registered); the failed request is structurally
    // WITHDRAWN — re-entering it is a NEW operator intent whose key
    // rotates via the reload epoch. NOTHING is replayed.
    void queryClient.refetchQueries({ queryKey: ["branches", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    create.reset();
    setNotice(
      "Register reloaded — the conflicted registration was withdrawn, nothing was replayed. A branch with this name (after whitespace normalisation) probably already exists.",
    );
    announce("Register reloaded. The conflicted registration was withdrawn; nothing was replayed.");
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (create.isPending || spent) return;
    const parsed = branchNameEntrySchema.safeParse({ name: name.trim() });
    if (!parsed.success) {
      setClientErrors({ name: parsed.error.issues[0]?.message ?? "Enter a branch name." });
      return;
    }
    setClientErrors({});
    create.mutate(parsed.data.name);
  }

  // Server 422 verdicts WIN over the client's guesses per field.
  const serverErrors = fromApiError(create.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    create.error instanceof ApiError &&
    create.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <Modal
      title="Register branch"
      onClose={onClose}
      closeDisabled={create.isPending}
      // W56-3: a stray overlay click must never discard a half-completed
      // registration — dismissal is the explicit ✕ or Escape.
      dismissOnOverlay={false}
    >
      {/* Informational, NOT success styling (W59-4). */}
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={create.error} onReload={reloadAfterConflict} />
      {create.isError && !conflict && !renderedInline && <ErrorBanner error={create.error} />}

      {spent && result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Branch registered</div>
          <Kv label="Branch">{result.name}</Kv>
          <Kv label="Record">
            <span className={styles.mono} title={result.id}>
              {result.id.slice(0, 8)}
            </span>
          </Kv>
          <Kv label="Registered">{fmtDateTime(result.created_at)}</Kv>
          <div className={styles.formNote}>
            The name above is the SERVER&apos;s stored record (whitespace
            normalised) — not this form&apos;s echo.
          </div>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose}>
              Close
            </Button>
            <Button
              type="button"
              variant="primary"
              onClick={() => {
                // A NEW intent: entry cleared; the next submission builds
                // fresh key material by content and intent counter.
                setResult(null);
                setName("");
                setClientErrors({});
                create.reset();
              }}
            >
              Register another
            </Button>
          </div>
        </div>
      )}

      {!spent && (
        <form onSubmit={submitEntry} noValidate>
          <FormField
            id="branch-name"
            label="Branch name"
            error={fieldErrors["name"]}
            hint="Up to 120 characters. The server strips surrounding whitespace and refuses a duplicate."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={create.isPending}
              />
            )}
          </FormField>
          <div className={styles.actions}>
            <Button type="button" onClick={onClose} disabled={create.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={create.isPending}>
              {create.isPending ? "Registering…" : "Register branch"}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
