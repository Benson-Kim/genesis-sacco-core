"use client";

import { useState } from "react";
import { Button } from "@genesis/design-system";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import { UsersScreen } from "@/modules/users/components/UsersScreen";
import { PermissionsScreen } from "@/modules/access/PermissionsScreen";
import styles from "./access-control.module.css";

/**
 * Client shell for the access-control page.
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
