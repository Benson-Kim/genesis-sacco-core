import type { Metadata } from "next";
import { RequireModule } from "@/modules/authz/components/RequireModule";
import { SettingsScreen } from "@/modules/settings/components/SettingsScreen";

export const metadata: Metadata = {
  title: "Settings · Genesis Prestige Admin",
};

/**
 * Settings — Interest / Loan products / Parameters / Approval matrix.
 * Static segment takes precedence over the scaffold's
 * [moduleId] placeholder. Deny-by-default: the route mounts only with
 * a settings:view grant (the API enforces every call regardless).
 */
export default function SettingsPage() {
  return (
    <RequireModule module="settings">
      <SettingsScreen />
    </RequireModule>
  );
}
