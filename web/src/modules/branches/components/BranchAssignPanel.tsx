"use client";

/**
 * Generic branch-assignment panel — ONE copy of
 * the assignment flow (reuse-first) parameterised over the two entity
 * kinds the contract exposes:
 *
 *   PUT /branches/{id}/users/{user_id} (access_control:edit)
 *   PUT /branches/{id}/members/{member_id} (members:edit)
 *
 * The registry/entity permission split is the router's recorded
 * decision: assigning a person to a branch is an edit of the PERSON's
 * record — settings rights alone must not allow it. The caller mounts
 * this panel only when the entity module's edit grant is held (pure UX; the server enforces regardless, least disclosure).
 *
 * - Picker options come from the entity module's own keyset list
 *   (scalability, explicit load-more; cached under a branches-scoped key so the mapped option shape can never collide with the entity module's canonical caches).
 * - FRESH ENTITY READ before the write (record class, staleTime 0;
 *   pattern (e)): the assignment pins the USER/MEMBER row's version —
 *   the write STRUCTURALLY refuses to fire without the loaded fresh
 *   record (the F-M1 class); a stale row is the server's 409 with
 *   NOTHING changed.
 * - EXACTLY ONE write per intent: pending short-circuit + disabled
 *   controls, one Idempotency-Key per logical intent — material folds
 *   the op discriminator, branch + entity ids, the FRESH entity
 *   version, a post-409 reload epoch and a per-success counter
 *   (README MATERIAL rules 3/4).
 * - A 409 (the person's record moved underneath) renders the shared
 *   ConflictBanner: reload refetches the fresh entity; NOTHING is
 *   replayed.
 * - The result panel renders the SERVER's assignment fact VERBATIM
 *   (BranchAssignmentOut) — entity, branch name, the row's new
 *   version.
 */
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Kv } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { isConflict } from "@/lib/errors";
import { STALE_TIME } from "@/lib/query";
import { useKeysetList } from "@/modules/table/useKeysetList";
import type { KeysetPage } from "@/modules/table/schemas";
import type { BranchAssignment } from "../schemas";
import styles from "./Branches.module.css";

/** Picker option mapped from the entity module's canonical record. */
export interface AssignOption {
  id: string;
  label: string;
}

/** Fresh entity read mapped from the entity module's canonical record. */
export interface AssignFreshEntity {
  id: string;
  version: number;
  label: string;
}

export interface AssignAdapter {
  /** Stable discriminator: "user" | "member" (ids, keys, op names). */
  kind: string;
  /** Panel heading, e.g. "Assign a user to this branch". */
  title: string;
  /** Field label, e.g. "User". */
  entityLabel: string;
  /** Idempotency op discriminator, e.g. "branch-assign-user". */
  op: string;
  /** Contract note rendered under the picker (permission honesty). */
  note: string;
  /** Entity keyset page mapped to options (the entity module's API). */
  fetchOptionsPage: (cursor: string | null) => Promise<KeysetPage<AssignOption>>;
  /** Fresh entity read (record class) supplying the pinned version. */
  fetchFresh: (id: string) => Promise<AssignFreshEntity>;
  /** The assignment write (branch + entity + pinned version). */
  assign: (
    branchId: string,
    entityId: string,
    version: number,
    idempotencyKey: string,
  ) => Promise<BranchAssignment>;
  /** Entity-module caches to invalidate after a successful write. */
  invalidateKeys: (entityId: string) => QueryKey[];
}

export function BranchAssignPanel({
  branchId,
  branchName,
  adapter,
}: Readonly<{
  branchId: string;
  branchName: string;
  adapter: AssignAdapter;
}>) {
  const queryClient = useQueryClient();
  const [entityId, setEntityId] = useState("");
  const [result, setResult] = useState<BranchAssignment | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const [intentSeq, setIntentSeq] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  // Branches-scoped option cache key: the mapped AssignOption shape
  // must never collide with the entity module's canonical list caches.
  const options = useKeysetList<AssignOption>({
    queryKey: ["branches", "assign-options", adapter.kind],
    fetchPage: (cursor) => adapter.fetchOptionsPage(cursor),
  });
  const optionRows = options.data?.pages.flatMap((page) => page.items) ?? [];

  // FRESH record read (staleTime 0) the moment an entity is chosen —
  // the write only arms once this read has landed (pattern (e)).
  const fresh = useQuery({
    queryKey: ["branches", "assign-fresh", adapter.kind, entityId === "" ? "none" : entityId],
    queryFn: () => adapter.fetchFresh(entityId),
    enabled: entityId !== "",
    staleTime: STALE_TIME.record,
  });
  const freshEntity = fresh.data;

  const assign = useMutation({
    // STRUCTURAL freshness guard (the F-M1 class): the version-pinned
    // write refuses to fire without the loaded fresh record — its key
    // material folds the REAL row version, never a null sentinel.
    mutationFn: () => {
      if (freshEntity === undefined) {
        return Promise.reject(
          new Error("The fresh record is not loaded — the assignment was NOT made."),
        );
      }
      return adapter.assign(
        branchId,
        freshEntity.id,
        freshEntity.version,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: adapter.op,
            branch_id: branchId,
            entity_id: freshEntity.id,
            entity_version: freshEntity.version,
            reload_epoch: reloadEpoch,
            intent_seq: intentSeq,
          }),
        ),
      );
    },
    onSuccess: (assignment) => {
      setResult(assignment);
      setIntentSeq((seq) => seq + 1);
      setNotice("");
      announce("Branch assignment recorded.");
      for (const key of adapter.invalidateKeys(assignment.entity_id)) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      void queryClient.invalidateQueries({
        queryKey: ["branches", "assign-fresh", adapter.kind, assignment.entity_id],
      });
    },
    onError: () => {
      announce("The branch assignment was NOT made.");
    },
  });

  const conflict = assign.isError && isConflict(assign.error);

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the fresh entity (its row
    // moved underneath); the failed request is structurally WITHDRAWN —
    // re-entering it is a NEW intent whose key rotates via the reload
    // epoch AND the reloaded version. NOTHING is replayed.
    void queryClient.refetchQueries({
      queryKey: ["branches", "assign-fresh", adapter.kind, entityId],
    });
    setReloadEpoch((epoch) => epoch + 1);
    assign.reset();
    setNotice(
      "Record reloaded — the conflicted assignment was withdrawn, nothing was replayed. Re-check the record before re-entering.",
    );
    announce("Record reloaded. The conflicted assignment was withdrawn; nothing was replayed.");
  }

  const fieldId = `assign-${adapter.kind}`;

  return (
    <div>
      <div className={styles.subhead}>{adapter.title}</div>
      {notice !== "" && <Banner>{notice}</Banner>}

      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={assign.error} onReload={reloadAfterConflict} />
      {assign.isError && !conflict && <ErrorBanner error={assign.error} />}

      {result !== null && (
        <div className={styles.resultPanel} role="status">
          <div className={styles.resultTitle}>Assignment recorded</div>
          <Kv label="Record">
            <span className={styles.mono} title={result.entity_id}>
              {result.entity_id.slice(0, 8)}
            </span>
          </Kv>
          <Kv label="Branch">{result.branch_name}</Kv>
          <Kv label="New record version">{result.version}</Kv>
          <div className={styles.formNote}>
            The facts above are the SERVER&apos;s assignment result — the
            branch name comes from the registry row, not this screen.
          </div>
        </div>
      )}

      <FormField id={fieldId} label={adapter.entityLabel}>
        {(control) => (
          <select
            {...control}
            className={styles.select}
            value={entityId}
            onChange={(event) => {
              setEntityId(event.target.value);
              setResult(null);
              assign.reset();
            }}
            disabled={assign.isPending}
          >
            <option value="">Select…</option>
            {optionRows.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {options.isError && <ErrorBanner error={options.error} />}
      {options.hasNextPage && (
        <Button
          type="button"
          onClick={() => void options.fetchNextPage()}
          disabled={options.isFetchingNextPage}
        >
          {options.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
      {entityId !== "" && fresh.isPending && (
        <div className={styles.formNote}>Verifying the record…</div>
      )}
      {entityId !== "" && fresh.isError && <ErrorBanner error={fresh.error} />}
      {freshEntity !== undefined && (
        <div className={styles.formNote}>
          Verified: {freshEntity.label} (record version {freshEntity.version}) — read fresh
          before this assignment.
        </div>
      )}
      <div className={styles.formNote}>{adapter.note}</div>
      <div className={styles.actions}>
        <Button
          type="button"
          variant="primary"
          onClick={() => {
            if (assign.isPending) return;
            // F-M1 belt: the write cannot even be attempted without
            // the loaded fresh record.
            if (freshEntity === undefined) return;
            assign.mutate();
          }}
          disabled={assign.isPending || freshEntity === undefined}
        >
          {assign.isPending ? "Assigning…" : `Assign ${adapter.kind} to ${branchName}`}
        </Button>
      </div>
    </div>
  );
}
