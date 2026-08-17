import { ApiError } from "@genesis/api-client";

/**
 * Least-disclosure operator-facing message per status (ported from !25 —
 * gate 1.6): no capability-probing hints, no server internals, no data
 * echo. 401/403/409/422 stay DISTINCT; the correlation id is the only
 * reference surfaced.
 */
const TITLES: Record<number, string> = {
  400: "Validation failed — check the highlighted fields.",
  401: "Session expired — sign in again.",
  403: "Not permitted.",
  404: "Not found.",
  409: "The record changed or conflicts with existing data — reload before retrying.",
  422: "Validation failed — check the highlighted fields.",
  429: "Too many attempts — wait a minute and retry.",
};

export interface OperatorMessage {
  title: string;
  correlationId: string | null;
}

export function operatorMessage(err: unknown): OperatorMessage {
  if (!(err instanceof ApiError)) {
    return { title: "Network error — check connectivity and retry.", correlationId: null };
  }
  return {
    title: TITLES[err.status] ?? "Something went wrong — retry or contact support.",
    correlationId: err.correlationId,
  };
}

export function isConflict(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409;
}
