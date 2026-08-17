import { notFound } from "next/navigation";
import { Card } from "@genesis/design-system";
import { RequireModule } from "@/modules/authz/components/RequireModule";
import { MODULES, MODULE_LABELS, isModuleId } from "@/modules/authz/modules";
import styles from "./page.module.css";

export function generateStaticParams() {
  // access_control has real screens (P13.5) served by its own static
  // route segment; every other module keeps the P15 placeholder.
  return MODULES.filter((moduleId) => moduleId !== "access_control").map((moduleId) => ({
    moduleId,
  }));
}

/**
 * Placeholder route per RBAC module, wrapped in the /me/permissions route
 * guard. The real screens are built in P15 on top of this scaffold.
 */
export default async function ModulePage({
  params,
}: {
  params: Promise<{ moduleId: string }>;
}) {
  const { moduleId } = await params;
  if (!isModuleId(moduleId)) {
    notFound();
  }
  return (
    <RequireModule module={moduleId}>
      <Card className={styles.placeholder}>
        <h2 className={styles.title}>{MODULE_LABELS[moduleId]}</h2>
        <p className={styles.body}>
          This module is scaffolded (route, guard, navigation). Its screens are
          delivered in P15 — Web admin features.
        </p>
      </Card>
    </RequireModule>
  );
}
