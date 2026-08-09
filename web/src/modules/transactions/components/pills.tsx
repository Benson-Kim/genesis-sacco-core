import type { ReactNode } from "react";
import { Pill } from "@genesis/design-system";
import {
  SIDE_LABELS,
  TXN_TYPE_LABELS,
  type Side,
  type TxnType,
} from "../schemas";

/**
 * Transaction type + direction pills (prototype `vTxn` colors, keyed on
 * the SERVER-reported direction — the client never re-derives which
 * side a type posts on). One copy for the register and the detail
 * drawer (reuse-first). Never color alone: the pill text carries the
 * meaning.
 */
export function txnTypePill(type: TxnType, direction: Side): ReactNode {
  if (direction === "debit") {
    return (
      <Pill bg="navySoft" color="brick">
        {TXN_TYPE_LABELS[type]}
      </Pill>
    );
  }
  return (
    <Pill bg="navySoft" color="emerald">
      {TXN_TYPE_LABELS[type]}
    </Pill>
  );
}

export function directionPill(direction: Side): ReactNode {
  if (direction === "debit") {
    return (
      <Pill bg="brickSoft" color="brick">
        {SIDE_LABELS.debit}
      </Pill>
    );
  }
  return (
    <Pill bg="emeraldSoft" color="emerald">
      {SIDE_LABELS.credit}
    </Pill>
  );
}

/** Reversing entries stay visible as first-class ledger facts (the
 * append-only ledger corrects by reversal, never by update). */
export function reversalPill(): ReactNode {
  return (
    <Pill bg="goldSoft" color="navy">
      Reversal
    </Pill>
  );
}
