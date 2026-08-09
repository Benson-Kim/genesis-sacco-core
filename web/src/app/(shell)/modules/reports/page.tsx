import { RequireModule } from "@/modules/authz/components/RequireModule";
import { PeriodContextNote } from "@/modules/periods/components/PeriodContextNote";
import { ReportsScreen } from "@/modules/reports/components/ReportsScreen";

/**
 * Reports module route (module 8). Static segment takes precedence
 * over the scaffold's [moduleId] placeholder. Deny-by-default guard
 * from /me/permissions; the API enforces every call regardless
 * (least disclosure).
 *
 * The period-context note ("statement readers have no period context") sits ABOVE the report
 * pickers. It SELF-GATES on transactions:view — a reports-only role
 * sees nothing and no periods endpoint is ever probed for it.
 */
export default function ReportsPage() {
    return (
        <RequireModule module="reports">
            <PeriodContextNote />
            <ReportsScreen />
        </RequireModule>
    );
}
