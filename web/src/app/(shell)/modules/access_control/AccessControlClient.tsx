"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import { Button, Card } from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import styles from "./access-control.module.css";

// Tab-level code splitting (speed): each panel is its own
// chunk — the permissions matrix never loads unless its tab is opened.
const UsersScreen = dynamic(
  () => import("@/modules/users/components/UsersScreen").then((m) => m.UsersScreen),
  { ssr: false, loading: () => <div className={styles.panelLoading}>Loading…</div> },
);
const PermissionsScreen = dynamic(
  () => import("@/modules/access/PermissionsScreen").then((m) => m.PermissionsScreen),
  { ssr: false, loading: () => <div className={styles.panelLoading}>Loading…</div> },
);

/**
 * Client shell for the access-control page (salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238).
 * - Tab switcher and "Add user" share one flex row; the button only
 *   appears when the Users tab is active AND the signed-in user has
 *   access_control:create.
 * - No decorative underline on the tab row — the card beneath provides
 *   the visual boundary.
 */
export function AccessControlClient() {
  const [tab, setTab] = useState<"users" | "permissions">("users");
  const permissions = usePermissions();
  const mayCreate = tab === "users" && can(permissions.data, "access_control", "create");

  // Passed down so UsersScreen can open the create drawer when the button
  // in this row is clicked.
  const [triggerCreate, setTriggerCreate] = useState(0);

  return (
    <div className={styles.page}>
        {/* Tab row with inline action button */}
        <div className={styles.tabsRow}>
          <div className={styles.tabs} role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={tab === "users"}
              className={`${styles.tab}${tab === "users" ? ` ${styles.tabActive}` : ""}`}
              onClick={() => setTab("users")}
            >
              Users
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "permissions"}
              className={`${styles.tab}${tab === "permissions" ? ` ${styles.tabActive}` : ""}`}
              onClick={() => setTab("permissions")}
            >
              Permissions
            </button>
          </div>

          {mayCreate && (
            <Button
              variant="primary"
              onClick={() => setTriggerCreate((n) => n + 1)}
            >
              + Add user
            </Button>
          )}
        </div>

        {/* Panel  */}
        {tab === "users" && <UsersScreen triggerCreate={triggerCreate} />}
        {tab === "permissions" && <PermissionsScreen />}
    </div>
  );
}
