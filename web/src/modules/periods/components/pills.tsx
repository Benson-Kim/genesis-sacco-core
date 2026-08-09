import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import { PERIOD_STATUS_LABELS, type PeriodStatus } from "../schemas";

/**
 * Period close-state pill. One copy for the
 * register and the context note (reuse-first). Never color alone: the
 * pill text carries the meaning.
 */
export function periodStatusPill(status: PeriodStatus): ReactNode {
  switch (status) {
    case "open":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {PERIOD_STATUS_LABELS.open}
        </Pill>
      );
    case "closed":
      return (
        <Pill bg="navySoft" color="navy">
          {PERIOD_STATUS_LABELS.closed}
        </Pill>
      );
  }
}
