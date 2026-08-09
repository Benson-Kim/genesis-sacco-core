import { RequireModule } from "@/modules/authz/components/RequireModule";
import { CommitteeScreen } from "@/modules/applications/components/CommitteeScreen";

/**
 * Credit-committee route (module 3 — prototype Governance ▸ Credit committee). Guarded by the applications module grant (deny by
 * default); vote affordances additionally require applications:approve
 * AND mount only through MakerCheckerPanel. The API enforces every call
 * regardless (least disclosure).
 */
export default function CommitteePage() {
    return (
        <RequireModule module="applications">
            <CommitteeScreen />
        </RequireModule>
    );
}
