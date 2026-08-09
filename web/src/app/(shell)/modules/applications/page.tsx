import { RequireModule } from "@/modules/authz/components/RequireModule";
import { ApplicationsScreen } from "@/modules/applications/components/ApplicationsScreen";

/**
 * Applications module route (module 3). Static segment takes
 * precedence over the scaffold's [moduleId] placeholder. Deny-by-default
 * guard from /me/permissions; the API enforces every call regardless
 * (least disclosure).
 */
export default function ApplicationsPage() {
    return (
        <RequireModule module="applications">
            <ApplicationsScreen />
        </RequireModule>
    );
}
