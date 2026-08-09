import { RequireModule } from "@/modules/authz/components/RequireModule";
import { ShareTransfersScreen } from "@/modules/shares/components/ShareTransfersScreen";

/**
 * Share-transfers route (remainder). Lives under the members RBAC module: the
 * /members/{id}/share-transfers route is gated members:approve
 * server-side (moving member equity — the settlement precedent; there is no dedicated shares module in the P4 matrix).
 * Deny-by-default guard from /me/permissions; the API enforces every
 * call regardless (least disclosure).
 */
export default function ShareTransfersPage() {
    return (
        <RequireModule module="members">
            <ShareTransfersScreen />
        </RequireModule>
    );
}
