import { RequireModule } from "@/modules/authz/components/RequireModule";
import { LoansScreen } from "@/modules/loans/components/LoansScreen";

/**
 * Loan-book module route (module 4). Static segment takes
 * precedence over the scaffold's [moduleId] placeholder. Deny-by-default
 * guard from /me/permissions; the API enforces every call regardless
 * (least disclosure).
 */
export default function LoanBookPage() {
    return (
        <RequireModule module="loan_book">
            <LoansScreen />
        </RequireModule>
    );
}
