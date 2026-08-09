import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import {
  ADJUSTMENT_STATUS_LABELS,
  WRITE_OFF_STATUS_LABELS,
  type AdjustmentStatus,
  type WriteOffStatus,
} from "../schemas";

/**
 * Corrections lifecycle status pills. One copy for the workbench and
 * the drawers (reuse-first). Never color alone: the pill text carries the
 * meaning. The write-off CLASSIFICATION pill is the loans
 * module's loanClassPill — reused, not re-drawn.
 */
export function adjustmentStatusPill(status: AdjustmentStatus): ReactNode {
  switch (status) {
    case "pending_approval":
      return (
        <Pill bg="goldSoft" color="navy">
          {ADJUSTMENT_STATUS_LABELS.pending_approval}
        </Pill>
      );
    case "posted":
      return (
        <Pill bg="emeraldSoft" color="emerald">
          {ADJUSTMENT_STATUS_LABELS.posted}
        </Pill>
      );
    case "rejected":
      return (
        <Pill bg="brickSoft" color="brick">
          {ADJUSTMENT_STATUS_LABELS.rejected}
        </Pill>
      );
  }
}

export function writeOffStatusPill(status: WriteOffStatus): ReactNode {
  switch (status) {
    case "requested":
      return (
        <Pill bg="goldSoft" color="navy">
          {WRITE_OFF_STATUS_LABELS.requested}
        </Pill>
      );
    case "approved":
      return (
        <Pill bg="navySoft" color="navy">
          {WRITE_OFF_STATUS_LABELS.approved}
        </Pill>
      );
    case "posted":
      return (
        <Pill bg="brickSoft" color="brick">
          {WRITE_OFF_STATUS_LABELS.posted}
        </Pill>
      );
    case "rejected":
      return <Pill>{WRITE_OFF_STATUS_LABELS.rejected}</Pill>;
  }
}
