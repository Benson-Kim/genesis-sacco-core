import { RequireModule } from "@/modules/authz/components/RequireModule";
import { MembersScreen } from "@/modules/members/components/MembersScreen";

/**
 * Members module route. Static segment takes precedence over the
 * scaffold's [moduleId] placeholder. Deny-by-default guard from
 * /me/permissions; the API enforces every call regardless (least disclosure).
 */
export default function MembersPage() {
    return (
        <RequireModule module="members">
            <MembersScreen />
        </RequireModule>
    );
}

