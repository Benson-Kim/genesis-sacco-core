"use client";

import { ApiError } from "@genesis/api-client";
import { Banner } from "@genesis/design-system";
import { operatorMessage } from "@/lib/errors";
import styles from "./ErrorBanner.module.css";

/**
 * Shared least-disclosure error banner (salvaged from the frontend branch @ 198a238): sanitized per-status title, the correlation id, and
 * 422 field-level messages from the toApiError parser — never figures,
 * balances or server internals (least disclosure). Everything renders as React
 * text nodes, so attacker-influenced field names/messages stay inert.
 */
export function ErrorBanner({ error }: Readonly<{ error: unknown }>) {
    const { title, correlationId } = operatorMessage(error);
    const fields = error instanceof ApiError ? error.fields : [];
    return (
        <Banner variant="error">
            {title}
            {fields.length > 0 && (
                <ul className={styles.fields}>
                    {fields.map((field, index) => (
                        <li key={`${field.field}-${index}`}>
                            {field.field}: {field.message}
                        </li>
                    ))}
                </ul>
            )}
            {correlationId !== null && correlationId !== "" && (
                <span className={styles.cid}>ref: {correlationId}</span>
            )}
        </Banner>
    );
}
