import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import { EXIT_STATUS_LABELS, type ExitStatus } from "../schemas";

/**
 * Exit lifecycle status pill (prototype status colors). One copy for
 * the register, the detail drawer and the statement (reuse-first). Never
 * color alone: the pill text carries the meaning.
 */
export function exitStatusPill(status: ExitStatus): ReactNode {
  switch (status) {
    case "requested":
      return (
        <Pill bg="goldSoft" color="navy">
          {EXIT_STATUS_LABELS.requested}
        </Pill>
      );
    case "approved":
      return (
        <Pill bg="navySoft" color="navy">
          {EXIT_STATUS_LABELS.approved}
        </Pill>
      );
    case "settled":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {EXIT_STATUS_LABELS.settled}
        </Pill>
      );
    case "rejected":
      return (
        <Pill bg="brickSoft" color="brick">
          {EXIT_STATUS_LABELS.rejected}
        </Pill>
      );
  }
}
