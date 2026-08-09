import { RequireModule } from "@/modules/authz/components/RequireModule";
import { ExitsScreen } from "@/modules/exits/components/ExitsScreen";

/**
 * Member exit module route (module 7 — the workflow). Lives
 * under the members RBAC module: every /member-exits route is gated on
 * members view/edit/approve server-side (there is no dedicated exit
 * module in the P4 matrix). Deny-by-default guard from
 * /me/permissions; the API enforces every call regardless (least disclosure).
 */
export default function MemberExitsPage() {
    return (
        <RequireModule module="members">
            <ExitsScreen />
        </RequireModule>
    );
}
