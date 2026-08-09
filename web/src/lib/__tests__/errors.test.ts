import { ApiError } from "@genesis/api-client";
import { isConflict, operatorMessage } from "../errors";

describe("operatorMessage — least disclosure (gate 1.6)", () => {
    it("keeps 401/403/409/422 distinct without echoing server detail", () => {
        expect(operatorMessage(new ApiError(401, "unauthenticated", "c-1")).title).toBe(
            "Session expired — sign in again.",
        );
        expect(operatorMessage(new ApiError(403, "forbidden", "c-2")).title).toBe("Not permitted.");
        expect(operatorMessage(new ApiError(409, "conflict", "c-3")).title).toContain(
            "changed or conflicts",
        );
        expect(operatorMessage(new ApiError(422, "validation_error", "c-4")).title).toContain(
            "Validation failed",
        );
    });

    it("surfaces the correlation id and nothing else from the envelope", () => {
        const message = operatorMessage(new ApiError(500, "internal_error", "corr-99"));
        expect(message.correlationId).toBe("corr-99");
        // The sanitized category itself is NOT leaked into operator copy.
        expect(message.title).not.toContain("internal_error");
    });

    it("maps non-ApiError failures to a generic network message", () => {
        const message = operatorMessage(new TypeError("fetch failed"));
        expect(message.title).toBe("Network error — check connectivity and retry.");
        expect(message.correlationId).toBeNull();
    });
});

describe("isConflict", () => {
    it("detects only HTTP 409", () => {
        expect(isConflict(new ApiError(409, "conflict", null))).toBe(true);
        expect(isConflict(new ApiError(422, "validation_error", null))).toBe(false);
        expect(isConflict(new Error("boom"))).toBe(false);
    });
});

