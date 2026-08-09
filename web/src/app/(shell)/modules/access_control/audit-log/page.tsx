import type { Metadata } from "next";
import { RequireModule } from "@/modules/authz/components/RequireModule";
import { AuditScreen } from "@/modules/audit/components/AuditScreen";

export const metadata: Metadata = {
  title: "Access control · Audit log · Genesis Prestige Admin",
};

/**
 * Audit-log viewer route over GET /audit-log (salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238). RequirePermission
 * access_control:view server-side; this route guard is UX only.
 */
export default function AuditLogPage() {
  return (
    <RequireModule module="access_control">
      <AuditScreen />
    </RequireModule>
  );
}
