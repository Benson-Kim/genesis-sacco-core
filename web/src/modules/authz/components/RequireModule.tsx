"use client";

import type { ReactNode } from "react";
import { Card } from "@genesis/design-system";
import { usePermissions } from "../usePermissions";
import { can } from "../schemas";
import type { ModuleId } from "../modules";
import { MODULE_LABELS } from "../modules";
import styles from "./RequireModule.module.css";

/**
 * Route guard driven by /me/permissions: renders children only when the
 * signed-in role can view the module. Deny by default — while loading or on
 * error, nothing privileged is shown. The API still authorizes every call
 * server-side (gate 1.6).
 */
export function RequireModule({ module, children }: Readonly<{ module: ModuleId; children: ReactNode }>) {
  const permissions = usePermissions();

  if (permissions.isPending) {
    return <div className={styles.note}>Checking permissions…</div>;
  }

  if (permissions.isError || !can(permissions.data, module, "view")) {
    return (
      <Card className={styles.denied} role="alert">
        <div className={styles.deniedTitle}>Access denied</div>
        <div className={styles.deniedBody}>
          Your role does not have permission to view {MODULE_LABELS[module]}. Contact a
          system administrator if you believe this is a mistake.
        </div>
      </Card>
    );
  }

  return <>{children}</>;
}
