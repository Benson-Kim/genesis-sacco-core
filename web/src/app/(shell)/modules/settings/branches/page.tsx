import { RequireModule } from "@/modules/authz/components/RequireModule";
import { BranchesScreen } from "@/modules/branches/components/BranchesScreen";

/**
 * Branches registry route (the registry console). Lives under the settings RBAC module: the registry CRUD
 * sits under settings view/create/edit per the P4 matrix (the
 * prototype manages branches from Settings); the people ASSIGNMENTS
 * inside the drawer are gated on their OWN modules
 * (access_control:edit / members:edit — the registry/entity split).
 * Deny-by-default guard from /me/permissions; the API enforces every
 * call regardless (least disclosure).
 */
export default function BranchesPage() {
    return (
        <RequireModule module="settings">
            <BranchesScreen />
        </RequireModule>
    );
}
