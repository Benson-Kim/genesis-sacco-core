"use client";

import { ApiError } from "@genesis/api-client";
import { Banner } from "@genesis/design-system";
import { operatorMessage } from "@/lib/errors";
import styles from "./ErrorBanner.module.css";

/**
 * Shared least-disclosure error banner: sanitized per-status title,
 * correlation id, and 422 field messages. Everything renders as React
 * text nodes — attacker-influenced field names/messages stay inert.
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
