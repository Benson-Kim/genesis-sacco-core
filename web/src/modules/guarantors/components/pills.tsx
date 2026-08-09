import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import { GUARANTEE_STATUS_LABELS, type GuaranteeStatus } from "../schemas";

/**
 * Guarantee status pills — SERVER-reported lifecycle only (pledged ->
 * active -> released; the client never advances a status locally). One
 * copy for the session panel, drawers and dialogs (reuse-first). Never
 * color alone: the pill text carries the meaning.
 */
export function guaranteeStatusPill(status: GuaranteeStatus): ReactNode {
  switch (status) {
    case "pledged":
      return (
        <Pill bg="goldSoft" color="navy">
          {GUARANTEE_STATUS_LABELS.pledged}
        </Pill>
      );
    case "active":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {GUARANTEE_STATUS_LABELS.active}
        </Pill>
      );
    default:
      return <Pill>{GUARANTEE_STATUS_LABELS[status]}</Pill>;
  }
}
