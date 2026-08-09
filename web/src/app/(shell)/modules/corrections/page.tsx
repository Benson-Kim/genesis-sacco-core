import { RequireModule } from "@/modules/authz/components/RequireModule";
import { CorrectionsScreen } from "@/modules/corrections/components/CorrectionsScreen";

/**
 * Corrections module route (follow-on, — the corrections/write-offs/recoveries console). Static segment takes precedence over the scaffold's [moduleId]
 * placeholder. Gated on the DEDICATED corrections RBAC module (never
 * generic transactions — the fraud channel carries its own permission
 * strings). Deny-by-default guard from /me/permissions; the API
 * enforces every call regardless (least disclosure).
 */
export default function CorrectionsPage() {
    return (
        <RequireModule module="corrections">
            <CorrectionsScreen />
        </RequireModule>
    );
}
