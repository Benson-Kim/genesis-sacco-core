import { RequireModule } from "@/modules/authz/components/RequireModule";
import { DormancyWorklistScreen } from "@/modules/members/components/DormancyWorklistScreen";

/**
 * Dormancy worklist route (the dormancy state console). Lives under the members RBAC module: the worklist is the
 * member register's own dormant filter (members:view) and the
 * operations run is members:edit server-side (there is no dedicated
 * dormancy module in the P4 matrix — the member-exit-under-members
 * precedent). Deny-by-default guard from /me/permissions; the API
 * enforces every call regardless (least disclosure).
 */
export default function DormancyWorklistPage() {
    return (
        <RequireModule module="members">
            <DormancyWorklistScreen />
        </RequireModule>
    );
}
