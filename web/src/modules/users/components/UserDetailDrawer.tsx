"use client";

/**
 * User detail drawer: profile view, optimistic-locked edit with the
 * explicit 409 "record changed — reload" flow (never a silent overwrite,
 * exactly one write attempt per submission), confirmed activate/suspend,
 * audited role assignment and OTP lifecycle actions that surface
 * side-effect COUNTS only — zero OTP disclosure anywhere (gate 1.6).
 */
import { useRef, useState, type SubmitEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Banner, Button, Field, Modal } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { getOwnUserId } from "@/modules/auth/session";
import { isConflict } from "@/lib/errors";
import { fmtDateTime, initials } from "@/lib/format";
import {
  assignUserRole,
  changeUserStatus,
  fetchUser,
  invalidateUserOtp,
  reenrolUserOtp,
  updateUser,
  type UpdateUserInput,
} from "../api";
import type { OtpReenrolResult, Role, User, UserStatus } from "../schemas";
import { statusPill } from "./UsersScreen";
import styles from "./Users.module.css";

function Kv({ label, children }: Readonly<{ label: string; children: ReactNode }>) {
  return (
    <div className={styles.kvRow}>
      <span className={styles.kvKey}>{label}</span>
      <span className={styles.kvVal}>{children}</span>
    </div>
  );
}

type ConfirmKind = "status" | "role" | "otp-invalidate" | "otp-reenrol";

export function UserDetailDrawer({
  userId,
  roles,
  onClose,
}: Readonly<{
  userId: string;
  roles: Role[];
  onClose: () => void;
}>) {
  const permissions = usePermissions();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmKind | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [reloadNotice, setReloadNotice] = useState(false);

  const detail = useQuery({
    queryKey: ["users", "detail", userId],
    queryFn: () => fetchUser(userId),
  });

  const title = editing ? "Edit profile" : "User detail";

  if (detail.isPending) {
    return (
      <Modal title={title} onClose={onClose}>
        <div className={styles.muted}>Loading…</div>
      </Modal>
    );
  }

  if (detail.isError || detail.data === undefined) {
    return (
      <Modal title={title} onClose={onClose}>
        <ErrorBanner error={detail.error} />
      </Modal>
    );
  }

  const user = detail.data;
  const own = getOwnUserId() !== null && getOwnUserId() === user.id;
  const mayEdit = can(permissions.data, "access_control", "edit");
  const suspendTarget: UserStatus = user.status === "active" ? "suspended" : "active";

  return (
    <Modal title={title} onClose={onClose}>
      {editing ? (
        <>
          {reloadNotice && (
            <Banner>Record reloaded — re-apply your change against the current values.</Banner>
          )}
          <EditForm
            key={`${user.id}:${user.version}`}
            user={user}
            onDone={(message) => {
              setEditing(false);
              setReloadNotice(false);
              setNotice(message);
            }}
            onReloaded={() => setReloadNotice(true)}
            onCancel={() => {
              setEditing(false);
              setReloadNotice(false);
            }}
          />
        </>
      ) : (
        <>
          {notice !== "" && <Banner variant="ok">{notice}</Banner>}
          <div className={styles.userCell}>
            <div className={styles.avatar} aria-hidden>
              {initials(user.full_name)}
            </div>
            <div>
              <div className={styles.cellStrong}>{user.full_name}</div>
              <div className={styles.cellSub}>{user.email}</div>
            </div>
          </div>
          <div className={styles.detailGrid}>
            <Kv label="Role">{user.role_name}</Kv>
            <Kv label="Branch">{user.branch ?? "—"}</Kv>
            <Kv label="Phone">{user.phone ?? "—"}</Kv>
            <Kv label="Status">{statusPill(user.status)}</Kv>
            <Kv label="Last active">{fmtDateTime(user.last_active_at)}</Kv>
            <Kv label="Record version">{user.version}</Kv>
          </div>
          {own && (
            <Banner>
              This is your own account. Role and status changes require another
              administrator (separation of duties).
            </Banner>
          )}
          {mayEdit && (
            <>
              <div className={styles.subhead}>Actions</div>
              <div className={styles.actions}>
                <Button
                  onClick={() => {
                    setNotice("");
                    setEditing(true);
                  }}
                >
                  Edit profile
                </Button>
                <Button disabled={own} onClick={() => setConfirm("role")}>
                  Change role
                </Button>
                <Button
                  disabled={own}
                  className={suspendTarget === "suspended" ? styles.danger : undefined}
                  variant={suspendTarget === "suspended" ? "danger" : "success"}
                  onClick={() => setConfirm("status")}
                >
                  {suspendTarget === "suspended" ? "Suspend" : "Activate"}
                </Button>
              </div>
              <div className={styles.subhead}>OTP / credentials</div>
              <div className={styles.actions}>
                <Button onClick={() => setConfirm("otp-invalidate")}>
                  Invalidate pending OTP
                </Button>
                <Button onClick={() => setConfirm("otp-reenrol")}>
                  Force OTP re-enrolment
                </Button>
              </div>
              <div className={styles.formNote}>
                OTP codes are never shown — these actions void pending challenges and
                (for re-enrolment) revoke sessions; the user signs in again via a
                fresh OTP.
              </div>
            </>
          )}
          {confirm !== null && (
            <ConfirmActionDialog
              kind={confirm}
              user={user}
              roles={roles}
              suspendTarget={suspendTarget}
              onClose={() => setConfirm(null)}
              onApplied={(message) => {
                setConfirm(null);
                setNotice(message);
              }}
            />
          )}
        </>
      )}
    </Modal>
  );
}

/**
 * Optimistic-locked profile edit. Sends the `version` the record was
 * loaded with; a 409 shows the explicit reload flow and NEVER auto-retries
 * with a fresher version (mutations have retry: 0 — exactly one write
 * attempt per submission).
 */
function EditForm({
  user,
  onDone,
  onReloaded,
  onCancel,
}: Readonly<{
  user: User;
  onDone: (notice: string) => void;
  onReloaded: () => void;
  onCancel: () => void;
}>) {
  const queryClient = useQueryClient();
  const [fullName, setFullName] = useState(user.full_name);
  const [email, setEmail] = useState(user.email);
  const [phone, setPhone] = useState(user.phone ?? "");
  const [branch, setBranch] = useState(user.branch ?? "");
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const update = useMutation({
    mutationFn: (input: UpdateUserInput) =>
      updateUser(
        user.id,
        input,
        idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "update", id: user.id, input })),
      ),
    onSuccess: (updated) => {
      queryClient.setQueryData(["users", "detail", user.id], updated);
      void queryClient.invalidateQueries({ queryKey: ["users", "list"] });
      onDone("Profile saved.");
    },
  });

  const conflict = update.isError && isConflict(update.error);

  async function reloadRecord() {
    // Explicit reload flow: fetch the current record; the EditForm remounts
    // (keyed on version) with the fresh values. The stale submission is never
    // replayed. Notice is hoisted to the parent so it survives the remount.
    await queryClient.refetchQueries({ queryKey: ["users", "detail", user.id] });
    onReloaded();
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (update.isPending) return;
    const input: UpdateUserInput = { version: user.version };
    const name = fullName.trim();
    const mail = email.trim();
    if (name !== "") input.full_name = name;
    if (mail !== "") input.email = mail;
    if (phone.trim() !== "") input.phone = phone.trim();
    if (branch.trim() !== "") input.branch = branch.trim();
    update.mutate(input);
  }

  return (
    <form onSubmit={submit}>
      {conflict && (
        <Banner variant="error">
          This record changed since you loaded it (or conflicts with an existing
          one). Your change was NOT applied.
          <div className={styles.actions}>
            <Button type="button" onClick={() => void reloadRecord()}>Reload record</Button>
          </div>
        </Banner>
      )}
      {update.isError && !conflict && <ErrorBanner error={update.error} />}
      <Field label="Full name" htmlFor="edit-name">
        <input
          id="edit-name"
          className={styles.input}
          maxLength={200}
          value={fullName}
          onChange={(event) => setFullName(event.target.value)}
        />
      </Field>
      <Field label="Email" htmlFor="edit-email">
        <input
          id="edit-email"
          className={styles.input}
          type="email"
          maxLength={254}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </Field>
      <Field label="Phone" htmlFor="edit-phone">
        <input
          id="edit-phone"
          className={styles.input}
          maxLength={32}
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
        />
      </Field>
      <Field label="Branch" htmlFor="edit-branch">
        <input
          id="edit-branch"
          className={styles.input}
          maxLength={120}
          value={branch}
          onChange={(event) => setBranch(event.target.value)}
        />
      </Field>
      <div className={styles.formNote}>
        Optimistic lock: saving against record version {user.version}. Leaving an
        optional field blank keeps its saved value — clearing a saved value is not
        yet supported by the API (!24 review note).
      </div>
      <Button variant="primary" type="submit" className={styles.wide} disabled={update.isPending}>
        {update.isPending ? "Saving…" : "Save changes"}
      </Button>
      <Button type="button" className={styles.wide} onClick={onCancel} disabled={update.isPending}>
        Cancel
      </Button>
    </form>
  );
}

/**
 * Confirmation dialogs for destructive/audited actions. Status and role
 * mutations send the loaded `version` (409 => reload flow); OTP actions
 * render side-effect counts ONLY.
 */
function ConfirmActionDialog({
  kind,
  user,
  roles,
  suspendTarget,
  onClose,
  onApplied,
}: Readonly<{
  kind: ConfirmKind;
  user: User;
  roles: Role[];
  suspendTarget: UserStatus;
  onClose: () => void;
  onApplied: (notice: string) => void;
}>) {
  const queryClient = useQueryClient();
  const [roleId, setRoleId] = useState(user.role_id);
  const [otpResult, setOtpResult] = useState<OtpReenrolResult | { voided_otp_challenges: number } | null>(null);
  const keySlot = useRef<IdempotencyKeySlot>({ key: null, body: null });

  const action = useMutation({
    mutationFn: async () => {
      if (kind === "status") {
        const body = { version: user.version, status: suspendTarget };
        return {
          kind,
          user: await changeUserStatus(
            user.id,
            body,
            idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "status", id: user.id, body })),
          ),
        } as const;
      }
      if (kind === "role") {
        const body = { version: user.version, role_id: roleId };
        return {
          kind,
          user: await assignUserRole(
            user.id,
            body,
            idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "role", id: user.id, body })),
          ),
        } as const;
      }
      if (kind === "otp-invalidate") {
        return {
          kind,
          counts: await invalidateUserOtp(
            user.id,
            idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "otp-invalidate", id: user.id })),
          ),
        } as const;
      }
      return {
        kind,
        counts: await reenrolUserOtp(
          user.id,
          idempotencyKeyFor(keySlot.current, JSON.stringify({ op: "otp-reenrol", id: user.id })),
        ),
      } as const;
    },
    onSuccess: (result) => {
      if (result.kind === "status" || result.kind === "role") {
        queryClient.setQueryData(["users", "detail", user.id], result.user);
        void queryClient.invalidateQueries({ queryKey: ["users", "list"] });
        onApplied("Change applied.");
        return;
      }
      setOtpResult(result.counts);
    },
  });

  const conflict = action.isError && isConflict(action.error);

  const titles: Record<ConfirmKind, string> = {
    status: suspendTarget === "suspended" ? "Suspend user" : "Activate user",
    role: "Assign role",
    "otp-invalidate": "Invalidate OTP challenges",
    "otp-reenrol": "Force re-enrolment",
  };

  return (
    <Modal
      title={titles[kind]}
      onClose={onClose}
      closeDisabled={action.isPending}
      variant="dialog"
    >
      {otpResult !== null ? (
        <>
          <Banner variant="ok">Done.</Banner>
          <div className={styles.detailGrid}>
            <Kv label="Voided OTP challenges">{otpResult.voided_otp_challenges}</Kv>
            {"revoked_refresh_tokens" in otpResult && (
              <Kv label="Revoked refresh tokens">{otpResult.revoked_refresh_tokens}</Kv>
            )}
          </div>
          <Button className={styles.wide} onClick={onClose}>
            Close
          </Button>
        </>
      ) : (
        <>
          {kind === "status" && (
            <>
              <Banner>
                {suspendTarget === "suspended"
                  ? "Suspending takes effect immediately: pending OTP challenges are voided and every session is revoked in the same transaction."
                  : "Activating restores sign-in for this user."}
              </Banner>
              <div className={styles.detailGrid}>
                <Kv label="User">{user.full_name}</Kv>
                <Kv label="Current status">{user.status}</Kv>
                <Kv label="New status">{suspendTarget}</Kv>
              </div>
            </>
          )}
          {kind === "role" && (
            <>
              <Banner>
                Role assignment is audited with before/after. The last active System
                Admin cannot be re-roled away.
              </Banner>
              <div className={styles.detailGrid}>
                <Kv label="User">{user.full_name}</Kv>
                <Kv label="Current role">{user.role_name}</Kv>
              </div>
              <Field label="New role" htmlFor="confirm-role">
                <select
                  id="confirm-role"
                  className={styles.select}
                  value={roleId}
                  onChange={(event) => setRoleId(event.target.value)}
                >
                  {roles.map((role) => (
                    <option key={role.id} value={role.id}>
                      {role.name}
                    </option>
                  ))}
                </select>
              </Field>
            </>
          )}
          {kind === "otp-invalidate" && (
            <>
              <Banner>
                Voids every pending OTP challenge for this user. No code is ever
                displayed — the user simply requests a fresh OTP at sign-in.
              </Banner>
              <div className={styles.detailGrid}>
                <Kv label="User">{user.full_name}</Kv>
              </div>
            </>
          )}
          {kind === "otp-reenrol" && (
            <>
              <Banner>
                Voids pending OTP challenges AND revokes every session, forcing a
                fresh OTP sign-in. Nothing secret is shown or sent from this console.
              </Banner>
              <div className={styles.detailGrid}>
                <Kv label="User">{user.full_name}</Kv>
              </div>
            </>
          )}
          {action.isError && <ErrorBanner error={action.error} />}
          {conflict && (
            <div className={styles.actions}>
              <Button
                onClick={() => {
                  // Explicit reload flow: fetch the current record, then the
                  // operator re-initiates the action from fresh state.
                  void queryClient.refetchQueries({ queryKey: ["users", "detail", user.id] });
                  onClose();
                }}
              >
                Reload record
              </Button>
            </div>
          )}
          <div className={styles.actions}>
            <Button onClick={onClose} disabled={action.isPending}>
              Cancel
            </Button>
            <Button
              variant={kind === "status" && suspendTarget === "active" ? "success" : "primary"}
              className={
                (kind === "status" && suspendTarget === "suspended") || kind === "otp-reenrol"
                  ? styles.danger
                  : undefined
              }
              onClick={() => {
                if (!action.isPending) action.mutate();
              }}
              disabled={action.isPending}
            >
              {action.isPending ? "Working…" : titles[kind]}
            </Button>
          </div>
        </>
      )}
    </Modal>
  );
}
