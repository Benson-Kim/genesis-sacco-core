import type { Metadata } from "next";
import { RequireModule } from "@/modules/authz/components/RequireModule";
import { AuditScreen } from "@/modules/audit/components/AuditScreen";

export const metadata: Metadata = {
  title: "Access control · Audit log · Genesis Prestige Admin",
};

/**
 * P13.5 — audit-log viewer over GET /audit-log (RequirePermission
 * access_control:view server-side; this route guard is UX only).
 */
export default function AuditLogPage() {
  return (
    <RequireModule module="access_control">
      <AuditScreen />
    </RequireModule>
  );
}
