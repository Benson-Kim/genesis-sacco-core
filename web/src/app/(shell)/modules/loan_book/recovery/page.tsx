import { RequireModule } from "@/modules/authz/components/RequireModule";
import { RecoveryScreen } from "@/modules/recovery/components/RecoveryScreen";

/**
 * Recovery worklist route (follow-on, — the collections & recovery worklist).
 * Lives under the loan_book RBAC module: every /recovery-cases route
 * is gated on loan_book view/create/edit server-side (there is no
 * dedicated recovery module in the P4 matrix — the member-exit-under-
 * members precedent). Deny-by-default guard from /me/permissions; the
 * API enforces every call regardless (least disclosure).
 */
export default function RecoveryPage() {
    return (
        <RequireModule module="loan_book">
            <RecoveryScreen />
        </RequireModule>
    );
}
