import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import {
  LOAN_CLASS_LABELS,
  LOAN_STATUS_LABELS,
  type LoanClass,
  type LoanStatus,
} from "../schemas";

/**
 * Loan status + classification pills (prototype `classify()` colors,
 * SERVER-computed classification only — the client never re-derives it
 * from days past due). One copy for the register, the detail drawer and
 * the portfolio strip (reuse-first). Never color alone: the pill text
 * carries the meaning.
 */
export function loanStatusPill(status: LoanStatus): ReactNode {
  switch (status) {
    case "active":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {LOAN_STATUS_LABELS.active}
        </Pill>
      );
    case "written_off":
      return (
        <Pill bg="brickSoft" color="brick">
          {LOAN_STATUS_LABELS.written_off}
        </Pill>
      );
    default:
      return <Pill>{LOAN_STATUS_LABELS[status]}</Pill>;
  }
}

export function loanClassPill(classification: LoanClass): ReactNode {
  switch (classification) {
    case "normal":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {LOAN_CLASS_LABELS.normal}
        </Pill>
      );
    case "watch":
      return (
        <Pill bg="goldSoft" color="navy">
          {LOAN_CLASS_LABELS.watch}
        </Pill>
      );
    case "substandard":
      return (
        <Pill bg="orangeSoft" color="orange">
          {LOAN_CLASS_LABELS.substandard}
        </Pill>
      );
    default:
      // doubtful | loss — the deep-arrears bands.
      return (
        <Pill bg="brickSoft" color="brick">
          {LOAN_CLASS_LABELS[classification]}
        </Pill>
      );
  }
}
