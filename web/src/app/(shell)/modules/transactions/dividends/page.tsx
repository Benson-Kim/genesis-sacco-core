import { RequireModule } from "@/modules/authz/components/RequireModule";
import { DividendsScreen } from "@/modules/dividends/components/DividendsScreen";

/**
 * Dividends lifecycle route (the declare/approve/distribute contract). Lives under the transactions
 * RBAC module: every /dividends route is gated on transactions
 * view/edit/approve server-side (there is no dedicated dividends
 * module in the P4 matrix). Deny-by-default guard from
 * /me/permissions; the API enforces every call regardless (least disclosure).
 */
export default function DividendsPage() {
    return (
        <RequireModule module="transactions">
            <DividendsScreen />
        </RequireModule>
    );
}
