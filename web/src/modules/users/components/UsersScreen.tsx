"use client";

/**
 * P13.5 users administration screen (prototype Access-control "Users" tab)
 * ported from !25 onto the P14 scaffold.
 *
 * Security posture (carried forward):
 * - Every rendered string (names, emails, branches) is attacker-influenced
 *   data; it renders exclusively through React text interpolation — no
 *   parser sink exists in this module (eslint-guarded + tested).
 * - UI affordances follow the P4 matrix via /me/permissions — pure UX;
 *   the server enforces every call (gate 1.6). 403 renders a generic
 *   "Not permitted." with a correlation id and no capability hints.
 * - Keyset pagination only (opaque cursors; no page numbers/offsets).
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, Pill } from "@genesis/design-system";
import { KeysetTable, type Column } from "@/modules/table/KeysetTable";
import { useKeysetList } from "@/modules/table/useKeysetList";
import { fmtDateTime, initials, relTime } from "@/lib/format";
import { fetchRoles, fetchUsersPage } from "../api";
import type { User } from "../schemas";
import { UserCreateDrawer } from "./UserCreateDrawer";
import { UserDetailDrawer } from "./UserDetailDrawer";
import styles from "./Users.module.css";

export const ROLES_QUERY_KEY = ["access", "roles"] as const;

export function useRoles() {
  // Role names degrade gracefully on failure; the list still works and a
  // role change would 409/422 server-side regardless (!25 finding 6).
  return useQuery({ queryKey: ROLES_QUERY_KEY, queryFn: fetchRoles });
}

export function statusPill(status: string) {
  if (status === "active") {
    return (
      <Pill bg="emeraldSoft" color="emerald">
        Active
      </Pill>
    );
  }
  if (status === "suspended") {
    return (
      <Pill bg="brickSoft" color="brick">
        Suspended
      </Pill>
    );
  }
  return <Pill>{status}</Pill>;
}

type DrawerState = null | { mode: "create" } | { mode: "detail"; userId: string };

export function UsersScreen({ triggerCreate = 0 }: { triggerCreate?: number }) {
  const roles = useRoles();
  const [drawer, setDrawer] = useState<DrawerState>(null);

  // Open the create drawer whenever the parent increments triggerCreate
  useEffect(() => {
    if (triggerCreate > 0) setDrawer({ mode: "create" });
  }, [triggerCreate]);

  const list = useKeysetList<User>({
    queryKey: ["users", "list"],
    fetchPage: (cursor) => fetchUsersPage({ status: "", roleId: "" }, cursor),
  });

  const columns: Column<User>[] = [
    {
      key: "user",
      header: "User",
      render: (user) => (
        <div className={styles.userCell}>
          <div className={styles.avatar} aria-hidden>
            {initials(user.full_name)}
          </div>
          <div>
            <div className={styles.cellStrong}>{user.full_name}</div>
            <div className={styles.cellSub}>{user.email}</div>
          </div>
        </div>
      ),
    },
    {
      key: "role",
      header: "Role",
      render: (user) => <Pill>{user.role_name}</Pill>,
    },
    {
      key: "branch",
      header: "Branch",
      render: (user) => <span className={styles.muted}>{user.branch ?? "—"}</span>,
    },
    {
      key: "last_active",
      header: "Last active",
      align: "right",
      render: (user) => (
        <span className={styles.muted} title={fmtDateTime(user.last_active_at)}>
          {relTime(user.last_active_at)}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      align: "right",
      render: (user) => statusPill(user.status),
    },
  ];

  return (
    <Card padded={false}>
      <KeysetTable
        columns={columns}
        query={list}
        rowKey={(user) => user.id}
        emptyMessage="No users found."
        onRowClick={(user) => setDrawer({ mode: "detail", userId: user.id })}
      />
      {drawer !== null && drawer.mode === "create" && (
        <UserCreateDrawer
          roles={roles.data ?? []}
          onClose={() => setDrawer(null)}
          onCreated={(user) => setDrawer({ mode: "detail", userId: user.id })}
        />
      )}
      {drawer !== null && drawer.mode === "detail" && (
        <UserDetailDrawer
          userId={drawer.userId}
          roles={roles.data ?? []}
          onClose={() => setDrawer(null)}
        />
      )}
    </Card>
  );
}
