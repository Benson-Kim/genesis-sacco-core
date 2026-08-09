import { toApiError } from "@genesis/api-client";
import { api } from "@/lib/api";
import { dashboardSummarySchema, type DashboardSummary } from "./schemas";

/**
 * Dashboard summary over the GENERATED client (no hand-written fetch — the house doctrine). Ungranted slices arrive omitted; the server is
 * the authorizer of every figure (least disclosure).
 */
export async function fetchDashboardSummary(): Promise<DashboardSummary> {
    const { data, error, response } = await api.GET("/dashboard/summary");
    if (error !== undefined || data === undefined) {
        throw toApiError(error, response);
    }
    return dashboardSummarySchema.parse(data);
}

