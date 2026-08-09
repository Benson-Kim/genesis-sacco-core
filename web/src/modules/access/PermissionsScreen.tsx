"use client";

/**
 * Permissions matrix (salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238) — left sidebar
 * lists roles; selecting one loads its per-module permission bits into a
 * table of checkboxes. Checkbox toggles edit a LOCAL draft; the save
 * button flushes every dirty module as one reviewed batch of
 * PUT /access/roles/{id}/permissions/{module} calls — nothing writes
 * until the operator commits.
 *
 * Security posture:
 * - Role names render through React text nodes only — no parser sink.
 * - Permission edits require access_control:edit (least disclosure); the server
 *   enforces this — the UI hides the checkboxes purely as UX.
 * - Every PUT is audit-logged server-side (data integrity) and carries an
 *   Idempotency-Key (concurrency safety) that is stable across retries of the same
 *   role/module/bits and rotates when the bits change.
 * - Failures render the sanitized least-disclosure banner
 *   ({category, correlation_id}) — never server internals.
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { idempotencyKeyFor, type IdempotencyKeySlot } from "@genesis/api-client";
import { Button, Card } from "@genesis/design-system";
import { ErrorBanner } from "@/modules/layout/ErrorBanner";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { MODULES, MODULE_LABELS, type ModuleId } from "@/modules/authz/modules";
import { STALE_TIME } from "@/lib/query";
import { fetchRoles } from "@/modules/users/api";
import { fetchRolePermissions, updateRolePermission, type Permission } from "./api";
import grid from "@/modules/layout/grid.module.css";
import styles from "./PermissionsScreen.module.css";

/**
 * Build a zero-map for a role that has no entry for a given module yet
 * (deny-by-default matches the server's RBAC policy).
 */
function emptyPermission(module: string): Permission {
  return { module, can_view: false, can_create: false, can_edit: false, can_approve: false };
}

const ACTIONS = [
  { key: "can_view" as const, label: "View" },
  { key: "can_create" as const, label: "Create" },
  { key: "can_edit" as const, label: "Edit / Post" },
  { key: "can_approve" as const, label: "Approve" },
];

export function PermissionsScreen() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const mayEdit = can(permissions.data, "access_control", "edit");

  const rolesQuery = useQuery({
    queryKey: ["access", "roles"],
    queryFn: fetchRoles,
    staleTime: STALE_TIME.reference,
  });

  const [selectedRoleId, setSelectedRoleId] = useState<string | null>(null);

  // Auto-select the first role once loaded
  const autoSelected = useRef(false);
  useEffect(() => {
    if (!autoSelected.current && rolesQuery.data && rolesQuery.data.length > 0) {
      setSelectedRoleId(rolesQuery.data[0]!.id);
      autoSelected.current = true;
    }
  }, [rolesQuery.data]);

  const permsQuery = useQuery({
    queryKey: ["access", "permissions", selectedRoleId],
    queryFn: () => fetchRolePermissions(selectedRoleId!),
    enabled: selectedRoleId !== null,
  });

  // Local draft — keyed by module id for O(1) lookup
  const [draft, setDraft] = useState<Record<string, Permission>>({});
  const [saveStatus, setSaveStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");

  // One Idempotency-Key slot per role/module cell: retries of an identical
  // {role, module, bits} submission reuse the key; changed bits rotate it
  // (concurrency safety — the idempotencyKeyFor contract).
  const keySlots = useRef<Record<string, IdempotencyKeySlot>>({});

  // When a new role is selected or server data arrives, reset the draft
  useEffect(() => {
    if (!permsQuery.data) return;
    const map: Record<string, Permission> = {};
    for (const p of permsQuery.data) map[p.module] = p;
    setDraft(map);
    setSaveStatus("idle");
  }, [permsQuery.data, selectedRoleId]);

  function getPermission(module: string): Permission {
    return draft[module] ?? emptyPermission(module);
  }

  function toggleBit(module: string, bit: keyof Omit<Permission, "module">) {
    if (!mayEdit) return;
    setSaveStatus("idle");
    setDraft((prev) => {
      const current = prev[module] ?? emptyPermission(module);
      return { ...prev, [module]: { ...current, [bit]: !current[bit] } };
    });
  }

  // Determine which modules have unsaved changes vs server state
  function dirtyModules(): ModuleId[] {
    if (!permsQuery.data) return [];
    const serverMap: Record<string, Permission> = {};
    for (const p of permsQuery.data) serverMap[p.module] = p;
    return (MODULES as readonly string[]).filter((m) => {
      const server = serverMap[m] ?? emptyPermission(m);
      const local = draft[m] ?? emptyPermission(m);
      return (
        server.can_view !== local.can_view ||
        server.can_create !== local.can_create ||
        server.can_edit !== local.can_edit ||
        server.can_approve !== local.can_approve
      );
    }) as ModuleId[];
  }

  const saveAll = useMutation({
    mutationFn: async () => {
      if (selectedRoleId === null) return;
      const dirty = dirtyModules();
      for (const moduleId of dirty) {
        const p = draft[moduleId] ?? emptyPermission(moduleId);
        const body = {
          can_view: p.can_view,
          can_create: p.can_create,
          can_edit: p.can_edit,
          can_approve: p.can_approve,
        };
        const slotKey = `${selectedRoleId}:${moduleId}`;
        const slot = (keySlots.current[slotKey] ??= { key: null, body: null });
        const updated = await updateRolePermission(
          selectedRoleId,
          moduleId,
          body,
          idempotencyKeyFor(
            slot,
            JSON.stringify({ op: "perm", role: selectedRoleId, module: moduleId, body }),
          ),
        );
        setDraft((prev) => ({ ...prev, [moduleId]: updated }));
      }
    },
    onMutate: () => setSaveStatus("saving"),
    onSuccess: () => {
      setSaveStatus("saved");
      void queryClient.invalidateQueries({ queryKey: ["access", "permissions", selectedRoleId] });
      // Signed-in user's own permissions may have changed
      void queryClient.invalidateQueries({ queryKey: ["me", "permissions"] });
      setTimeout(() => setSaveStatus("idle"), 3000);
    },
    onError: () => setSaveStatus("error"),
  });

  const selectedRole = rolesQuery.data?.find((r) => r.id === selectedRoleId);
  const dirty = dirtyModules();
  const hasDirty = dirty.length > 0;

  const statusText =
    saveStatus === "saving"
      ? "Saving…"
      : saveStatus === "saved"
        ? `Saved ${selectedRole?.name ?? ""}`
        : saveStatus === "error"
          ? "Save failed — retry"
          : hasDirty
            ? `${dirty.length} unsaved change${dirty.length > 1 ? "s" : ""}`
            : "";

  return (
    // Shared responsive side+main grid (grid.module.css): stacks <=960px.
    <div className={grid.sideMain}>
      {/* Role sidebar */}
      <Card padded={false}>
        {/* Toggle buttons with aria-pressed — conforming semantics for a
            single-select side list (review finding: role=listbox with
            interposed <li> broke the required owned relationship). */}
        <ul className={styles.roleList} aria-label="Roles">
          {rolesQuery.isPending &&
            Array.from({ length: 5 }).map((_, i) => (
              <li key={i} className={`${styles.roleItem} ${styles.roleSkeleton}`}>
                <span className={styles.roleIcon} />
                &nbsp;
              </li>
            ))}
          {rolesQuery.isError && (
            <li>
              <ErrorBanner error={rolesQuery.error} />
            </li>
          )}
          {(rolesQuery.data ?? []).map((role) => (
            <li key={role.id}>
              <button
                type="button"
                aria-pressed={role.id === selectedRoleId}
                className={`${styles.roleItem}${role.id === selectedRoleId ? ` ${styles.active}` : ""}`}
                onClick={() => {
                  if (role.id !== selectedRoleId) {
                    setSelectedRoleId(role.id);
                    setSaveStatus("idle");
                  }
                }}
              >
                <span className={styles.roleIcon} aria-hidden />
                {role.name}
              </button>
            </li>
          ))}
        </ul>
      </Card>

      {/* ── Matrix panel ── */}
      <Card padded={false}>
        <div className={styles.matrixHead}>
          {selectedRole
            ? `Permissions — ${selectedRole.name}`
            : "Select a role"}
        </div>

        {selectedRoleId === null || permsQuery.isPending ? (
          <div className={styles.placeholder}>
            {selectedRoleId === null ? "Select a role to view permissions." : "Loading…"}
          </div>
        ) : (
          <>
            <table className={styles.matrixTable}>
              <thead>
                <tr>
                  <th className={styles.matrixTh}>Module</th>
                  {ACTIONS.map((a) => (
                    <th key={a.key} className={styles.matrixTh}>
                      {a.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {MODULES.map((module) => {
                  const perm = getPermission(module);
                  return (
                    <tr key={module}>
                      <td className={styles.matrixTd}>{MODULE_LABELS[module]}</td>
                      {ACTIONS.map((a) => (
                        <td key={a.key} className={styles.matrixTd}>
                          {/* >=44px hit target around the 24px visual box */}
                          <label className={styles.checkTarget}>
                            <input
                              type="checkbox"
                              className={styles.check}
                              checked={perm[a.key]}
                              disabled={!mayEdit || saveAll.isPending}
                              aria-label={`${MODULE_LABELS[module]} — ${a.label}`}
                              onChange={() => toggleBit(module, a.key)}
                            />
                          </label>
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {saveAll.isError && (
              <div className={styles.placeholder}>
                <ErrorBanner error={saveAll.error} />
              </div>
            )}
            {mayEdit && (
              <div className={styles.matrixFooter}>
                <span
                  className={`${styles.saveStatus}${saveStatus === "error" ? ` ${styles.error}` : saveStatus === "saved" ? ` ${styles.ok}` : ""}`}
                >
                  {statusText}
                </span>
                <Button
                  variant="primary"
                  disabled={!hasDirty || saveAll.isPending}
                  onClick={() => {
                    if (!saveAll.isPending) saveAll.mutate();
                  }}
                >
                  {saveAll.isPending ? "Saving…" : "Save role"}
                </Button>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
