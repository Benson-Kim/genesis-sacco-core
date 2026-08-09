"""Member exit & settlement domain: status machine and settlement arithmetic.

Zero I/O — no imports from application, infrastructure, or api layers.
Money rounding comes from genesis.domain.money (reuse-first).

Status machine (mirrors the 0001 member_exits CHECK constraint; concurrency safety):

    requested -> approved | rejected committee quorum (P9 machinery)
    approved -> settled | rejected settled by the atomic posting;
                                       rejected = void (the approved
                                       snapshot drifted, or governance
                                       reversed the decision)
    settled / rejected terminal

Settlement arithmetic (single source of truth, persisted verbatim to
the member_exits snapshot row):

    equity = shares + deposits
    loan_total = principal balance + interest due + penalties
                  (the early-settlement rule per loan: interest on installments not yet due is
                  waived, never collected)
    net_payable = equity - loan_total - exit fee

Negative-settlement rule (documented): a settlement whose net_payable
would be negative — the loan payoff exceeds the member's equity — is
REJECTED at request time. The SACCO never auto-claims guarantor
collateral or funds it does not hold; the member must first reduce the
loan (deposits stay open to arrears members) until the netted
settlement is non-negative. The prototype's "call guarantors"
shortfall path is a manual recovery workflow outside.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from genesis.domain.money import ZERO, to_cents


class ExitStatus(enum.StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    SETTLED = "settled"
    REJECTED = "rejected"


class InvalidExitTransitionError(Exception):
    """Raised when an exit settlement transition is not allowed (concurrency safety)."""


_ALLOWED: dict[ExitStatus, frozenset[ExitStatus]] = {
    ExitStatus.REQUESTED: frozenset({ExitStatus.APPROVED, ExitStatus.REJECTED}),
    ExitStatus.APPROVED: frozenset({ExitStatus.SETTLED, ExitStatus.REJECTED}),
    ExitStatus.SETTLED: frozenset(),
    ExitStatus.REJECTED: frozenset(),
}


def exit_transition(current: ExitStatus, target: ExitStatus) -> ExitStatus:
    """The single gatekeeper for exit settlement status changes (concurrency safety)."""
    if target not in _ALLOWED[current]:
        raise InvalidExitTransitionError(f"{current.value} -> {target.value}")
    return target


@dataclass(frozen=True)
class SettlementComputation:
    """One member's exit settlement, computed from cent-rounded components.

    Every component is non-negative; the sign of the settlement lives
    only in net_payable. The same computation runs at request time (to
    persist the snapshot) and again at posting time under the full lock
    set (to re-verify the snapshot — the quote/approve/post TOCTOU
    defence): both sides compare component by component.
    """

    shares: Decimal
    deposits: Decimal
    loan_principal: Decimal
    loan_interest: Decimal
    loan_penalties: Decimal
    fee: Decimal

    @property
    def equity(self) -> Decimal:
        return to_cents(self.shares + self.deposits)

    @property
    def loan_total(self) -> Decimal:
        return to_cents(self.loan_principal + self.loan_interest + self.loan_penalties)

    @property
    def net_payable(self) -> Decimal:
        return to_cents(self.equity - self.loan_total - self.fee)


def compute_settlement(
    *,
    shares: Decimal,
    deposits: Decimal,
    loan_principal: Decimal,
    loan_interest: Decimal,
    loan_penalties: Decimal,
    fee: Decimal,
) -> SettlementComputation:
    """Build a settlement computation; every component must be non-negative."""
    computation = SettlementComputation(
        shares=to_cents(shares),
        deposits=to_cents(deposits),
        loan_principal=to_cents(loan_principal),
        loan_interest=to_cents(loan_interest),
        loan_penalties=to_cents(loan_penalties),
        fee=to_cents(fee),
    )
    for name in ("shares", "deposits", "loan_principal", "loan_interest", "loan_penalties", "fee"):
        if getattr(computation, name) < ZERO:
            raise ValueError(f"settlement component {name} must not be negative")
    return computation
