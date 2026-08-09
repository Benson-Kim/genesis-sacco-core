"use client";

/**
 * Branch detail drawer (the registry console): the fresh branch record plus the three writes the
 * contract exposes against it — the version-pinned RENAME
 * (settings:edit) and the two people ASSIGNMENTS, each behind its OWN
 * module's edit grant (access_control:edit for users, members:edit for members — the registry/entity split recorded on the router; the server enforces regardless, least disclosure).
 *
 * - FRESH branch read (record class, staleTime 0): the rename pins
 *   the freshest version — drift since the operator's read is a 409
 *   with NOTHING changed, rendered via the shared ConflictBanner's
 *   explicit reload-and-re-enter flow (never replayed, never a
 *   silent overwrite).
 * - Rename key material folds the op discriminator + the full
 *   canonical body INCLUDING the pinned version + a post-409 reload
 *   epoch (README MATERIAL rule 3): a reload that advanced the
 *   version is a NEW intent.
 * - CONTRACT HONESTY: the read contract exposes NO roster (no
 *   "users/members of this branch" list) and NO delete/deactivate —
 *   neither is faked here; recorded on as contract
 *   follow-ups.
 */
import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Kv, Modal } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { ConflictBanner } from "@/modules/layout/ConflictBanner";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { announce } from "@/modules/layout/announcer";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { isConflict } from "@/lib/errors";
import { fmtDateTime } from "@/lib/format";
import { STALE_TIME } from "@/lib/query";
import { fetchUser, fetchUsersPage } from "@/modules/users/api";
import { fetchMember, fetchMembersPage } from "@/modules/members/api";
import { STATUS_LABELS } from "@/modules/members/schemas";
import {
  assignMemberToBranch,
  assignUserToBranch,
  fetchBranch,
  fetchBranchMembersRosterPage,
  fetchBranchUsersRosterPage,
  updateBranch,
} from "../api";
import {
  branchNameEntrySchema,
  type BranchMemberRosterRow,
  type BranchRecord,
  type BranchUserRosterRow,
} from "../schemas";
import { BranchAssignPanel, type AssignAdapter } from "./BranchAssignPanel";
import { BranchRosterPanel, type RosterAdapter } from "./BranchRosterPanel";
import styles from "./Branches.module.css";

/** USER roster adapter ((j).1 — access_control:view, the assignment split mirrored on reads). Identity facts only, rendered
 * as inert React text. */
const userRosterAdapter: RosterAdapter<BranchUserRosterRow> = {
  kind: "users",
  title: "Users assigned to this branch",
  note:
    "Reading the USER roster is user administration (access_control:view — the read leg of the assignment permission split), never a settings right. Identity facts only; the full user record stays on the users screen.",
  columns: [
    {
      key: "user",
      header: "User",
      render: (row) => (
        <div>
          <div>{row.full_name}</div>
          <div className={styles.muted}>{row.email}</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      align: "right",
      render: (row) => (row.status === "active" ? "Active" : "Suspended"),
    },
  ],
  fetchPage: fetchBranchUsersRosterPage,
  rowKey: (row) => row.id,
  emptyMessage: "No users are assigned to this branch yet.",
};

/** MEMBER roster adapter ((j).1 — members:view). */
const memberRosterAdapter: RosterAdapter<BranchMemberRosterRow> = {
  kind: "members",
  title: "Members assigned to this branch",
  note:
    "Reading the MEMBER roster is a member read (members:view — the read leg of the assignment permission split), never a settings right. Identity facts only; figures stay on the members register.",
  columns: [
    {
      key: "member",
      header: "Member",
      render: (row) => (
        <div>
          <div>{row.name}</div>
          <div className={styles.muted}>{row.member_no}</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      align: "right",
      render: (row) => STATUS_LABELS[row.status],
    },
  ],
  fetchPage: fetchBranchMembersRosterPage,
  rowKey: (row) => row.id,
  emptyMessage: "No members are assigned to this branch yet.",
};

/** User-assignment adapter (access_control:edit — the users precedent). Options/fresh reads come from the users module's own
 * API layer (its Zod boundary included). */
const userAdapter: AssignAdapter = {
  kind: "user",
  title: "Assign a user to this branch",
  entityLabel: "User",
  op: "branch-assign-user",
  note:
    "Assigning a USER is user administration (access_control:edit), not a settings right. The write pins the user row's fresh version.",
  fetchOptionsPage: async (cursor) => {
    const page = await fetchUsersPage({ status: "", roleId: "" }, cursor);
    return {
      items: page.items.map((user) => ({
        id: user.id,
        label: `${user.full_name} · ${user.email}`,
      })),
      nextCursor: page.nextCursor,
    };
  },
  fetchFresh: async (id) => {
    const user = await fetchUser(id);
    return { id: user.id, version: user.version, label: `${user.full_name} · ${user.email}` };
  },
  assign: (branchId, entityId, version, idempotencyKey) =>
    assignUserToBranch(branchId, entityId, version, idempotencyKey),
  invalidateKeys: (entityId) => [
    ["users", "list"],
    ["users", "detail", entityId],
  ],
};

/** Member-assignment adapter (members:edit — the precedent). */
const memberAdapter: AssignAdapter = {
  kind: "member",
  title: "Assign a member to this branch",
  entityLabel: "Member",
  op: "branch-assign-member",
  note:
    "Assigning a MEMBER is a member edit (members:edit — the P8 precedent), not a settings right. The write pins the member row's fresh version.",
  fetchOptionsPage: async (cursor) => {
    const page = await fetchMembersPage({ status: "", type: "" }, cursor);
    return {
      items: page.items.map((member) => ({
        id: member.id,
        label: `${member.name} · ${member.member_no}`,
      })),
      nextCursor: page.nextCursor,
    };
  },
  fetchFresh: async (id) => {
    const member = await fetchMember(id);
    return { id: member.id, version: member.version, label: `${member.name} · ${member.member_no}` };
  },
  assign: (branchId, entityId, version, idempotencyKey) =>
    assignMemberToBranch(branchId, entityId, version, idempotencyKey),
  invalidateKeys: (entityId) => [
    ["members", "list"],
    ["members", "detail", entityId],
  ],
};

/** Version-pinned rename form — mounted keyed on the fresh record's
 * version, so a post-conflict reload re-seeds the input from the
 * reloaded truth. */
function RenameForm({ branch }: Readonly<{ branch: BranchRecord }>) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(branch.name);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [notice, setNotice] = useState<string>("");
  const [reloadEpoch, setReloadEpoch] = useState(0);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const rename = useMutation({
    mutationFn: (newName: string) =>
      updateBranch(
        branch.id,
        branch.version,
        newName,
        idempotencyKeyFor(
          keySlot.current,
          JSON.stringify({
            op: "branch-rename",
            id: branch.id,
            body: { version: branch.version, name: newName },
            reload_epoch: reloadEpoch,
          }),
        ),
      ),
    onSuccess: (record) => {
      setNotice("");
      announce("Branch renamed.");
      // The register and the fresh record are server facts — refetch.
      void queryClient.invalidateQueries({ queryKey: ["branches", "list"] });
      void queryClient.invalidateQueries({ queryKey: ["branches", "detail", record.id] });
    },
    onError: () => {
      announce("The branch was NOT renamed.");
    },
  });

  const conflict = rename.isError && isConflict(rename.error);

  function reloadAfterConflict() {
    // Explicit reload flow: refetch the branch (renamed or
    // re-registered by a concurrent operator — the pinned version is
    // stale, or the new name collides); the failed request is
    // structurally WITHDRAWN. NOTHING is replayed; the reloaded record
    // re-seeds this form via the version-keyed remount.
    void queryClient.refetchQueries({ queryKey: ["branches", "detail", branch.id] });
    void queryClient.invalidateQueries({ queryKey: ["branches", "list"] });
    setReloadEpoch((epoch) => epoch + 1);
    rename.reset();
    setNotice(
      "Record reloaded — the conflicted rename was withdrawn, nothing was replayed. The record may have moved, or the name may already be in use.",
    );
    announce("Record reloaded. The conflicted rename was withdrawn; nothing was replayed.");
  }

  function submitRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (rename.isPending) return;
    const parsed = branchNameEntrySchema.safeParse({ name: name.trim() });
    if (!parsed.success) {
      setClientErrors({ name: parsed.error.issues[0]?.message ?? "Enter a branch name." });
      return;
    }
    setClientErrors({});
    rename.mutate(parsed.data.name);
  }

  const serverErrors = fromApiError(rename.error);
  const fieldErrors = mergeFieldErrors(clientErrors, serverErrors);
  const renderedInline =
    rename.error instanceof ApiError &&
    rename.error.status === 422 &&
    Object.keys(serverErrors).length > 0;

  return (
    <form onSubmit={submitRename} noValidate>
      <div className={styles.subhead}>Rename branch</div>
      {notice !== "" && <Banner>{notice}</Banner>}
      {/* One copy of the 409 reload-and-re-enter flow (reuse-first). */}
      <ConflictBanner error={rename.error} onReload={reloadAfterConflict} />
      {rename.isError && !conflict && !renderedInline && <ErrorBanner error={rename.error} />}
      <FormField
        id="branch-rename"
        label="Branch name"
        error={fieldErrors["name"]}
        hint={`Pinned to record version ${branch.version} — a concurrent change is refused, never overwritten.`}
      >
        {(control) => (
          <input
            {...control}
            className={styles.input}
            maxLength={120}
            value={name}
            onChange={(event) => setName(event.target.value)}
            disabled={rename.isPending}
          />
        )}
      </FormField>
      <div className={styles.actions}>
        <Button type="submit" variant="primary" disabled={rename.isPending}>
          {rename.isPending ? "Renaming…" : "Rename branch"}
        </Button>
      </div>
    </form>
  );
}

export function BranchDetailDrawer({
  branchId,
  onClose,
}: Readonly<{
  branchId: string;
  onClose: () => void;
}>) {
  const permissions = usePermissions();

  // Record class (staleTime 0): NEVER a stale snapshot into the
  // version-pinned rename.
  const detail = useQuery({
    queryKey: ["branches", "detail", branchId],
    queryFn: () => fetchBranch(branchId),
    staleTime: STALE_TIME.record,
  });

  const mayRename = can(permissions.data, "settings", "edit");
  const mayAssignUser = can(permissions.data, "access_control", "edit");
  const mayAssignMember = can(permissions.data, "members", "edit");
  // Roster READS ((j).1): the assignment permission split
  // mirrored — the entity being READ decides the gate. Withholding is
  // STRUCTURAL: without the grant the panel never mounts and the
  // roster endpoint is never probed (least disclosure; server enforces
  // regardless).
  const mayViewUserRoster = can(permissions.data, "access_control", "view");
  const mayViewMemberRoster = can(permissions.data, "members", "view");

  if (detail.isPending) {
    return (
      <Modal title="Branch" onClose={onClose}>
        <div className={styles.muted}>Loading branch…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title="Branch" onClose={onClose}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const record = detail.data;

  return (
    <Modal title="Branch" onClose={onClose} dismissOnOverlay={false}>
      <div className={styles.detailGrid}>
        <Kv label="Branch">{record.name}</Kv>
        <Kv label="Record">
          <span className={styles.mono} title={record.id}>
            {record.id.slice(0, 8)}
          </span>
        </Kv>
        <Kv label="Registered">{fmtDateTime(record.created_at)}</Kv>
        <Kv label="Record version">{record.version}</Kv>
      </div>
      <div className={styles.formNote}>
        The read contract exposes no delete/deactivate for a branch — none is
        invented here. Roster reads below follow the assignment permission
        split:
        each mounts only under its OWN module&apos;s view grant.
      </div>

      {/* Rosters ((j).1): mounted ONLY under their respective view
          grants — settings rights alone mount neither panel and
          neither roster endpoint is probed (structural withholding,
          jest-proven). */}
      {mayViewUserRoster && (
        <BranchRosterPanel branchId={record.id} adapter={userRosterAdapter} />
      )}
      {mayViewMemberRoster && (
        <BranchRosterPanel branchId={record.id} adapter={memberRosterAdapter} />
      )}

      {mayRename && <RenameForm key={record.version} branch={record} />}

      {mayAssignUser && (
        <BranchAssignPanel branchId={record.id} branchName={record.name} adapter={userAdapter} />
      )}
      {mayAssignMember && (
        <BranchAssignPanel branchId={record.id} branchName={record.name} adapter={memberAdapter} />
      )}

      {!mayRename && !mayAssignUser && !mayAssignMember && (
        <div className={styles.formNote}>
          You hold no edit grant for this record: renaming needs settings
          edit; assigning people needs access-control edit (users) or members
          edit (members). The server enforces this regardless.
        </div>
      )}
    </Modal>
  );
}
