"use client";

/**
 * Permissions matrix — left sidebar lists roles; selecting one loads its
 * per-module permission bits into a table where each cell is a checkbox.
 * Every checkbox change fires PUT /access/roles/{id}/permissions/{module}
 * immediately (no "Save" needed per cell — the save button batches
 * unsaved local changes and flushes them all at once for roles that need
 * reviewing before committing).
 *
 * Security posture:
 * - Role names render through React text nodes only — no parser sink.
 * - Permission edits require access_control:edit (gate 1.6); the server
 *   enforces this — the UI hides the checkboxes purely as UX.
 * - Every PUT is audit-logged server-side (gate 1.5).
 */
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card } from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { MODULES, MODULE_LABELS, type ModuleId } from "@/modules/authz/modules";
import { fetchRoles } from "@/modules/users/api";
import { fetchRolePermissions, updateRolePermission, type Permission } from "./api";
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
        const updated = await updateRolePermission(selectedRoleId, moduleId, {
          can_view: p.can_view,
          can_create: p.can_create,
          can_edit: p.can_edit,
          can_approve: p.can_approve,
        });
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
    <div className={styles.layout}>
      {/* Role sidebar */}
      <Card padded={false}>
        <ul className={styles.roleList} role="listbox" aria-label="Roles">
          {rolesQuery.isPending &&
            Array.from({ length: 5 }).map((_, i) => (
              <li key={i} className={styles.roleItem} style={{ opacity: 0.4 }}>
                <span className={styles.roleIcon} />
                &nbsp;
              </li>
            ))}
          {(rolesQuery.data ?? []).map((role) => (
            <li key={role.id}>
              <button
                type="button"
                role="option"
                aria-selected={role.id === selectedRoleId}
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
                          <input
                            type="checkbox"
                            className={styles.check}
                            checked={perm[a.key]}
                            disabled={!mayEdit || saveAll.isPending}
                            aria-label={`${MODULE_LABELS[module]} — ${a.label}`}
                            onChange={() => toggleBit(module, a.key)}
                          />
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {mayEdit && (
              <div className={styles.matrixFooter}>
                <span
                  className={`${styles.saveStatus}${saveStatus === "error" ? ` ${styles.error}` : saveStatus === "saved" ? ` ${styles.ok}` : ""}`}
                >
                  {statusText}
                </span>
                <Button
                  variant="primary"
                  loading={saveAll.isPending}
                  disabled={!hasDirty || saveAll.isPending}
                  onClick={() => saveAll.mutate()}
                >
                  Save role
                </Button>
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
