import { RequireModule } from "@/modules/authz/components/RequireModule";
import { PeriodContextNote } from "@/modules/periods/components/PeriodContextNote";
import { TransactionsScreen } from "@/modules/transactions/components/TransactionsScreen";

/**
 * Transactions module route (module 6). Static segment takes
 * precedence over the scaffold's [moduleId] placeholder. Deny-by-default
 * guard from /me/permissions; the API enforces every call regardless
 * (least disclosure).
 *
 * The period-context note sits
 * ABOVE the register: period-close state next to the figures operators
 * read. It self-gates on transactions:view (trivially held here).
 */
export default function TransactionsPage() {
    return (
        <RequireModule module="transactions">
            <PeriodContextNote />
            <TransactionsScreen />
        </RequireModule>
    );
}
