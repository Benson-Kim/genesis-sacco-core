import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import { CASE_STATUS_LABELS, type CaseStatus } from "../schemas";

/**
 * Recovery-case lifecycle status pill. One copy for the worklist and
 * the case drawer (reuse-first). Never color alone: the pill text carries
 * the meaning. The loan CLASSIFICATION pill is the loans
 * module's loanClassPill — reused, not re-drawn.
 */
export function caseStatusPill(status: CaseStatus): ReactNode {
  switch (status) {
    case "open":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {CASE_STATUS_LABELS.open}
        </Pill>
      );
    case "disputed":
    case "irrecoverable_pending_write_off":
      return (
        <Pill bg="goldSoft" color="navy">
          {CASE_STATUS_LABELS[status]}
        </Pill>
      );
    case "closed_cured":
      return (
        <Pill bg="navySoft" color="navy">
          {CASE_STATUS_LABELS.closed_cured}
        </Pill>
      );
    case "closed_written_off":
      return (
        <Pill bg="brickSoft" color="brick">
          {CASE_STATUS_LABELS.closed_written_off}
        </Pill>
      );
    case "closed_restructured":
      return <Pill>{CASE_STATUS_LABELS.closed_restructured}</Pill>;
  }
}
