import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import { DECLARATION_STATUS_LABELS, type DeclarationStatus } from "../schemas";

/**
 * Declaration lifecycle status pill (prototype status colors). One
 * copy for the register and the drawers (reuse-first). Never color
 * alone: the pill text carries the meaning.
 */
export function declarationStatusPill(status: DeclarationStatus): ReactNode {
  switch (status) {
    case "declared":
      return (
        <Pill bg="goldSoft" color="navy">
          {DECLARATION_STATUS_LABELS.declared}
        </Pill>
      );
    case "approved":
      return (
        <Pill bg="navySoft" color="navy">
          {DECLARATION_STATUS_LABELS.approved}
        </Pill>
      );
    case "distributed":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {DECLARATION_STATUS_LABELS.distributed}
        </Pill>
      );
    case "rejected":
      return (
        <Pill bg="brickSoft" color="brick">
          {DECLARATION_STATUS_LABELS.rejected}
        </Pill>
      );
  }
}
