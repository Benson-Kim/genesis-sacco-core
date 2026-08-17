import type { Metadata } from "next";
import { RequireModule } from "@/modules/authz/components/RequireModule";
import { AccessControlClient } from "./AccessControlClient";

export const metadata: Metadata = {
  title: "Access control · Genesis Prestige Admin",
};

/**
 * Access control — Users table + Permissions matrix.
 * Metadata lives here (server component); tab state lives in the client child.
 */
export default function AccessControlPage() {
  return (
    <RequireModule module="access_control">
      <AccessControlClient />
    </RequireModule>
  );
}
