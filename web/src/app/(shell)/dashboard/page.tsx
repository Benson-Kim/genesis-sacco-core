import { DashboardScreen } from "@/modules/dashboard/components/DashboardScreen";

/**
 * Dashboard — live figures from GET /dashboard/summary. Every
 * amount is the API's decimal string; the client computes nothing.
 * Slices the caller's role is not granted arrive
 * omitted and render as "—" (deny-by-default, least disclosure).
 */
export default function DashboardPage() {
    return <DashboardScreen />;
}
