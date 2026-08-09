import { RequireModule } from "@/modules/authz/components/RequireModule";
import { GuarantorsScreen } from "@/modules/guarantors/components/GuarantorsScreen";

/**
 * Guarantors route (module 5 — prototype Operations ▸ Guarantors).
 * Guarded by the applications module grant (deny by default): every
 * guarantee endpoint (pledge/consent/release/substitute) is gated on
 * the applications module server-side (P9/), and the
 * guarantor aggregates slice is granted with applications:view. Pledge
 * and lifecycle affordances additionally require applications:edit.
 * The API enforces every call regardless (least disclosure).
 */
export default function GuarantorsPage() {
    return (
        <RequireModule module="applications">
            <GuarantorsScreen />
        </RequireModule>
    );
}
