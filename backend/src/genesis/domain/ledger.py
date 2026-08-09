# ruff: noqa: I001
"""Double-entry ledger domain: pure types, account chart, and posting rules.

Zero I/O — no imports from application, infrastructure, or api layers.
Money rounding comes from genesis.domain.money (reuse-first).

Every posting is a balanced set of LedgerLine pairs (DR == CR).
Corrections are reversing entries only — UPDATE/DELETE are forbidden by
DB trigger (data integrity).

Reference prefixes (concurrency safety):
  MP- M-Pesa deposit
  BK- Bank-channel deposit (addition to the house doctrine base set: bank deposits are cash inflows,
  so reusing WD-/INT- would misclassify them; BK- keeps deposit references channel-distinguishable)
  LN- Loan disbursement
  RP- Loan repayment
  SH- Share top-up
  WD- Withdrawal (any channel) / exit settlement
  INT- Interest posting (loan accrual and deposit interest)

Deposits are only valid on the MPESA and BANK channels; ACCRUAL/INTERNAL
deposits raise ValueError (interest is posted via the dedicated interest
factories, never as a deposit).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from genesis.domain.money import ZERO, to_cents


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TxnType(enum.StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    SHARE_TOPUP = "share_topup"
    LOAN_DISBURSEMENT = "loan_disbursement"
    LOAN_REPAYMENT = "loan_repayment"
    INTEREST_POSTING = "interest_posting"
    EXIT_SETTLEMENT = "exit_settlement"
    DIVIDEND_POSTING = "dividend_posting"
    SHARE_TRANSFER_OUT = "share_transfer_out"
    SHARE_TRANSFER_IN = "share_transfer_in"
    # misc fee (FE-) and the write-off provisioning posting
    # (WO-); values match the 0025 transactions.type CHECK.
    FEE = "fee"
    LOAN_WRITE_OFF = "loan_write_off"
    # Issue (follow-up): bad-debt recovery receipt (RC-)
    # against a written-off loan's surviving claim; value matches the
    # 0030 transactions.type CHECK.
    LOAN_RECOVERY = "loan_recovery"


class Channel(enum.StrEnum):
    MPESA = "mpesa"
    BANK = "bank"
    ACCRUAL = "accrual"
    INTERNAL = "internal"


class Side(enum.StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


# ---------------------------------------------------------------------------
# Chart of accounts (symbolic names; stored as text in the DB)
# ---------------------------------------------------------------------------


class Account(enum.StrEnum):
    # Asset accounts
    CASH_MPESA = "cash.mpesa"
    CASH_BANK = "cash.bank"
    LOANS_RECEIVABLE = "loans.receivable"
    INTEREST_RECEIVABLE = "interest.receivable"

    # Liability accounts
    MEMBER_DEPOSITS = "member.deposits"
    MEMBER_SHARES = "member.shares"
    # Unclaimed dividends payable (P3): the record-date
    # entitlement of a member who EXITED between declaration and
    # distribution is parked here as an explicit, reportable liability
    # — never credited to member accounts, never a silent shortfall.
    # Resolved only through the correction paths (reversing
    # entries), never UPDATE/DELETE.
    UNCLAIMED_DIVIDENDS = "liability.unclaimed_dividends"

    # Income accounts
    INTEREST_INCOME = "income.interest"
    PENALTY_INCOME = "income.penalties"
    FEE_INCOME = "income.fees"
    # Issue: cash recovered against a written-off loan's surviving
    # claim is recognised as bad-debt recovery INCOME (the accounting
    # treatment named on the issue) — never a reversal of the WO-
    # posting and never a resurrection of loans.receivable. The claim
    # itself lives in the write-once loan_write_offs snapshot; partial
    # recoveries are tracked by the append-only loan_recoveries rows.
    RECOVERY_INCOME = "income.bad_debt_recoveries"

    # Expense accounts
    INTEREST_EXPENSE = "interest.expense"
    DIVIDEND_EXPENSE = "expense.dividends"
    REBATE_EXPENSE = "expense.rebates"
    # bad-debt expense recognised when a committee-approved
    # write-off derecognises loans.receivable. Provisions are loan-row
    # bookkeeping (loans.provision_pct), never ledger balances, so the
    # write-off charge posts directly to expense (documented in
    # build_write_off_posting).
    WRITE_OFF_EXPENSE = "expense.loan_writeoffs"

    # Clearing account for member-to-member share transfers:
    # the OUT and IN legs post as two member-attributed transactions
    # so each member's ledger-reconstructed share history stays exact
    # (the ADB basis keys legs to members via transactions.member_id);
    # this account nets to zero inside every transfer's transaction.
    SHARE_TRANSFER_CLEARING = "clearing.share_transfers"

    # Suspense / clearing (exceptional items only — never a structural leg)
    SUSPENSE = "suspense"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerLine:
    """One leg of a double-entry posting."""

    account: Account
    side: Side
    amount: Decimal

    def __post_init__(self) -> None:
        if self.amount <= ZERO:
            raise ValueError(f"LedgerLine amount must be positive, got {self.amount!r}")


@dataclass(frozen=True)
class PostingSpec:
    """A balanced set of ledger lines for one transaction.

    Invariant: sum(DR amounts) == sum(CR amounts).
    Validated by assert_balanced(); the DB trigger enforces this too.
    """

    txn_type: TxnType
    channel: Channel
    amount: Decimal
    lines: tuple[LedgerLine, ...]

    def __post_init__(self) -> None:
        if self.amount <= ZERO:
            raise ValueError(f"PostingSpec amount must be positive, got {self.amount!r}")
        if not self.lines:
            raise ValueError("PostingSpec must have at least one line")

    def assert_balanced(self) -> None:
        """Raise if DR total != CR total (data integrity)."""
        dr = sum((ln.amount for ln in self.lines if ln.side is Side.DEBIT), ZERO)
        cr = sum((ln.amount for ln in self.lines if ln.side is Side.CREDIT), ZERO)
        if dr != cr:
            raise ValueError(f"Unbalanced posting: DR={dr} CR={cr} diff={dr - cr}")


# ---------------------------------------------------------------------------
# Reference prefix registry
# ---------------------------------------------------------------------------

REF_PREFIX: dict[TxnType, str] = {
    TxnType.DEPOSIT: "MP-",  # channel-resolved via _DEPOSIT_CHANNEL_PREFIX
    TxnType.WITHDRAWAL: "WD-",
    TxnType.SHARE_TOPUP: "SH-",
    TxnType.LOAN_DISBURSEMENT: "LN-",
    TxnType.LOAN_REPAYMENT: "RP-",
    TxnType.INTEREST_POSTING: "INT-",
    TxnType.EXIT_SETTLEMENT: "WD-",  # exit settlement is a withdrawal variant
    TxnType.DIVIDEND_POSTING: "DV-",
    TxnType.SHARE_TRANSFER_OUT: "ST-",  # both transfer legs share the ST-
    TxnType.SHARE_TRANSFER_IN: "ST-",  # sequence (the WD- precedent)
    TxnType.FEE: "FE-",  #  misc fees
    TxnType.LOAN_WRITE_OFF: "WO-",  #  write-off provisioning posting
    TxnType.LOAN_RECOVERY: "RC-",  #  bad-debt recovery receipts
}

# Channel-specific prefix for deposits. Deposits only arrive via M-Pesa or
# bank; ACCRUAL/INTERNAL deposits are invalid (interest uses the dedicated
# interest factories with the INT- prefix).
_DEPOSIT_CHANNEL_PREFIX: dict[Channel, str] = {
    Channel.MPESA: "MP-",
    Channel.BANK: "BK-",
}


def ref_prefix(txn_type: TxnType, channel: Channel) -> str:
    """Return the reference prefix for a given transaction type and channel."""
    if txn_type is TxnType.DEPOSIT:
        try:
            return _DEPOSIT_CHANNEL_PREFIX[channel]
        except KeyError:
            raise ValueError(
                f"deposits are only valid on MPESA or BANK channels, got {channel!r}"
            ) from None
    return REF_PREFIX[txn_type]


# ---------------------------------------------------------------------------
# Posting factory functions (pure — no I/O)
# ---------------------------------------------------------------------------


def _cash_account(channel: Channel) -> Account:
    """Cash account for a channel on which money physically moves.

    ACCRUAL/INTERNAL are refused instead of silently routing to
    suspense (external Codex review, re-derived): a structural cash
    leg parked in suspense would hide a mis-channelled posting inside
    the exceptional-items account, defeating reconciliation (data integrity).
    The API boundary already rejects non-cash channels
    (api.params.require_cash_channel); this is the defence in depth
    for internal callers and future code paths.
    """
    if channel is Channel.MPESA:
        return Account.CASH_MPESA
    if channel is Channel.BANK:
        return Account.CASH_BANK
    raise ValueError(f"cash postings require MPESA or BANK channels, got {channel!r}")


def build_deposit_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """DR cash / CR member deposits.

    Only MPESA and BANK channels are valid — interest is never posted as a
    deposit (use the interest factories instead).
    """
    if channel not in _DEPOSIT_CHANNEL_PREFIX:
        raise ValueError(f"deposits are only valid on MPESA or BANK channels, got {channel!r}")
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.DEPOSIT,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=_cash_account(channel), side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.MEMBER_DEPOSITS, side=Side.CREDIT, amount=amt),
        ),
    )


def build_withdrawal_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """DR member deposits / CR cash."""
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.WITHDRAWAL,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=Account.MEMBER_DEPOSITS, side=Side.DEBIT, amount=amt),
            LedgerLine(account=_cash_account(channel), side=Side.CREDIT, amount=amt),
        ),
    )


def build_share_topup_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """DR cash / CR member shares."""
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.SHARE_TOPUP,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=_cash_account(channel), side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.MEMBER_SHARES, side=Side.CREDIT, amount=amt),
        ),
    )


def build_disbursement_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """DR loans receivable / CR cash (funds leave the SACCO)."""
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.LOAN_DISBURSEMENT,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=Account.LOANS_RECEIVABLE, side=Side.DEBIT, amount=amt),
            LedgerLine(account=_cash_account(channel), side=Side.CREDIT, amount=amt),
        ),
    )


def build_repayment_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """DR cash / CR loans receivable."""
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.LOAN_REPAYMENT,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=_cash_account(channel), side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.LOANS_RECEIVABLE, side=Side.CREDIT, amount=amt),
        ),
    )


def build_allocated_repayment_posting(
    penalties: Decimal,
    interest: Decimal,
    principal: Decimal,
    channel: Channel,
) -> PostingSpec:
    """Split-leg repayment posting for the allocation contract.

    DR cash for the full amount received; CR one leg per non-zero
    allocation component:
      * penalties -> income.penalties
      * interest -> income.interest (recognised on receipt; the accrual job owns interest.receivable
      postings)
      * principal -> loans.receivable
    Balanced by construction; at least one component must be positive.
    """
    penalties = to_cents(penalties)
    interest = to_cents(interest)
    principal = to_cents(principal)
    if penalties < ZERO or interest < ZERO or principal < ZERO:
        raise ValueError("allocation components must not be negative")
    total = to_cents(penalties + interest + principal)
    if total <= ZERO:
        raise ValueError("allocated repayment must have a positive total")
    lines: list[LedgerLine] = [
        LedgerLine(account=_cash_account(channel), side=Side.DEBIT, amount=total)
    ]
    if penalties > ZERO:
        lines.append(LedgerLine(account=Account.PENALTY_INCOME, side=Side.CREDIT, amount=penalties))
    if interest > ZERO:
        lines.append(LedgerLine(account=Account.INTEREST_INCOME, side=Side.CREDIT, amount=interest))
    if principal > ZERO:
        lines.append(
            LedgerLine(account=Account.LOANS_RECEIVABLE, side=Side.CREDIT, amount=principal)
        )
    return PostingSpec(
        txn_type=TxnType.LOAN_REPAYMENT,
        channel=channel,
        amount=total,
        lines=tuple(lines),
    )


def build_loan_interest_accrual_posting(amount: Decimal) -> PostingSpec:
    """Accrue interest earned on a loan: DR interest receivable / CR interest income."""
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.INTEREST_POSTING,
        channel=Channel.ACCRUAL,
        amount=amt,
        lines=(
            LedgerLine(account=Account.INTEREST_RECEIVABLE, side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.INTEREST_INCOME, side=Side.CREDIT, amount=amt),
        ),
    )


def build_deposit_interest_posting(amount: Decimal) -> PostingSpec:
    """Interest paid on member deposits: DR interest expense / CR member deposits."""
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.INTEREST_POSTING,
        channel=Channel.ACCRUAL,
        amount=amt,
        lines=(
            LedgerLine(account=Account.INTEREST_EXPENSE, side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.MEMBER_DEPOSITS, side=Side.CREDIT, amount=amt),
        ),
    )


def build_exit_settlement_posting(
    *,
    shares: Decimal,
    deposits: Decimal,
    loan_principal: Decimal,
    loan_interest: Decimal,
    loan_penalties: Decimal,
    fee: Decimal,
    channel: Channel,
) -> PostingSpec:
    """Exit settlement set-off: one balanced posting, no suspense leg.

    DR member.shares + DR member.deposits — the member equity being
    extinguished; CR loans.receivable / income.interest /
    income.penalties — the loan payoff netted inside the settlement
    (the early-settlement split: principal, interest already due, penalties); CR income.fees — the
    tenant-configured exit fee;
    CR cash — the net refund actually paid out.

    Balanced by construction: net = equity - loan components - fee, so
    DR(shares + deposits) == CR(loan + fee + net). Guards:
      * every component must be non-negative
      * a negative net settlement can never be posted (the documented rule rejects it at request
      time; this is the final gate)
      * a positive net refund requires a cash channel (MPESA/BANK);
        a zero-net set-off moves no cash and posts as INTERNAL
      * zero-equity settlements have nothing to post — the caller
        skips the ledger entirely (ValueError here is the guard)
    """
    shares = to_cents(shares)
    deposits = to_cents(deposits)
    loan_principal = to_cents(loan_principal)
    loan_interest = to_cents(loan_interest)
    loan_penalties = to_cents(loan_penalties)
    fee = to_cents(fee)
    if min(shares, deposits, loan_principal, loan_interest, loan_penalties, fee) < ZERO:
        raise ValueError("exit settlement components must not be negative")
    equity = to_cents(shares + deposits)
    net = to_cents(equity - loan_principal - loan_interest - loan_penalties - fee)
    if net < ZERO:
        raise ValueError("a negative exit settlement cannot be posted")
    if equity <= ZERO:
        raise ValueError("exit settlement with zero equity has nothing to post")
    lines: list[LedgerLine] = []
    if shares > ZERO:
        lines.append(LedgerLine(account=Account.MEMBER_SHARES, side=Side.DEBIT, amount=shares))
    if deposits > ZERO:
        lines.append(LedgerLine(account=Account.MEMBER_DEPOSITS, side=Side.DEBIT, amount=deposits))
    if loan_principal > ZERO:
        lines.append(
            LedgerLine(account=Account.LOANS_RECEIVABLE, side=Side.CREDIT, amount=loan_principal)
        )
    if loan_interest > ZERO:
        lines.append(
            LedgerLine(account=Account.INTEREST_INCOME, side=Side.CREDIT, amount=loan_interest)
        )
    if loan_penalties > ZERO:
        lines.append(
            LedgerLine(account=Account.PENALTY_INCOME, side=Side.CREDIT, amount=loan_penalties)
        )
    if fee > ZERO:
        lines.append(LedgerLine(account=Account.FEE_INCOME, side=Side.CREDIT, amount=fee))
    if net > ZERO:
        # Zero-net set-offs post as INTERNAL with no cash leg; a
        # positive refund must name the cash account it leaves through.
        if channel not in (Channel.MPESA, Channel.BANK):
            raise ValueError("a net exit refund requires a cash channel (mpesa or bank)")
        lines.append(LedgerLine(account=_cash_account(channel), side=Side.CREDIT, amount=net))
    return PostingSpec(
        txn_type=TxnType.EXIT_SETTLEMENT,
        channel=channel,
        amount=equity,
        lines=tuple(lines),
    )


def build_dividend_distribution_posting(
    dividend: Decimal, rebate: Decimal, *, credit_account: Account | None = None
) -> PostingSpec:
    """One member's dividend + rebate payout: DV- ref, ACCRUAL.

    Default (credit_account=None — byte-identical to the earlier behaviour): DR expense.dividends /
    CR member.shares for the
    dividend component (capitalised into share capital, so it compounds into the NEXT financial
    year's share basis — data integrity)
    and DR expense.rebates / CR member.deposits for the rebate
    component.

    Since (human-authorized on, 2026-08-07) the
    distribution engine may route BOTH member-side credit legs to a
    single retention destination chosen by the member's stored
    dividend_payout preference: credit_account is a CODE-OWNED value
    restricted to member.deposits or member.shares (never
    caller-supplied, never a cash account — external channels are
    unbuilt and are never faked; the engine falls back to the default
    with credit_account=None). The DR legs keep their expense
    classification either way, so the legs stay balanced and the
    declared totals conserve exactly.

    Components are rounded upstream by the single money primitive; at
    least one must be positive (zero entitlements are skipped, never
    posted).
    """
    if credit_account is not None and credit_account not in (
        Account.MEMBER_DEPOSITS,
        Account.MEMBER_SHARES,
    ):
        raise ValueError("dividend credit destination must be a member retention account")
    dividend_credit = Account.MEMBER_SHARES if credit_account is None else credit_account
    rebate_credit = Account.MEMBER_DEPOSITS if credit_account is None else credit_account
    dividend = to_cents(dividend)
    rebate = to_cents(rebate)
    if dividend < ZERO or rebate < ZERO:
        raise ValueError("dividend components must not be negative")
    total = to_cents(dividend + rebate)
    if total <= ZERO:
        raise ValueError("a zero dividend distribution cannot be posted")
    lines: list[LedgerLine] = []
    if dividend > ZERO:
        lines.append(LedgerLine(account=Account.DIVIDEND_EXPENSE, side=Side.DEBIT, amount=dividend))
        lines.append(LedgerLine(account=dividend_credit, side=Side.CREDIT, amount=dividend))
    if rebate > ZERO:
        lines.append(LedgerLine(account=Account.REBATE_EXPENSE, side=Side.DEBIT, amount=rebate))
        lines.append(LedgerLine(account=rebate_credit, side=Side.CREDIT, amount=rebate))
    return PostingSpec(
        txn_type=TxnType.DIVIDEND_POSTING,
        channel=Channel.ACCRUAL,
        amount=total,
        lines=tuple(lines),
    )


def build_unclaimed_dividend_posting(dividend: Decimal, rebate: Decimal) -> PostingSpec:
    """Mid-run-exit disposition of one member's entitlement (P3).

    DV- ref, ACCRUAL channel — the same transaction type as a paid
    distribution (dividend_posting stays classified SYSTEM in MEMBER_INITIATED, so the dormancy
    clock is untouched), but
    the credit legs park the money as an explicit
    liability.unclaimed_dividends payable instead of member accounts:
    DR expense.dividends / CR liability.unclaimed_dividends and
    DR expense.rebates / CR liability.unclaimed_dividends. The exited
    member holds no balances any more; recognising the payable keeps
    SUM(expense postings) == the approved declaration totals (the zero-residue conservation rule)
    while the disposition row, its
    audit row and the outbox event make the position visible.
    Resolution (pay-out or write-back) happens through the
    correction paths as reversing entries.
    """
    dividend = to_cents(dividend)
    rebate = to_cents(rebate)
    if dividend < ZERO or rebate < ZERO:
        raise ValueError("dividend components must not be negative")
    total = to_cents(dividend + rebate)
    if total <= ZERO:
        raise ValueError("a zero unclaimed dividend cannot be posted")
    lines: list[LedgerLine] = []
    if dividend > ZERO:
        lines.append(LedgerLine(account=Account.DIVIDEND_EXPENSE, side=Side.DEBIT, amount=dividend))
        lines.append(
            LedgerLine(account=Account.UNCLAIMED_DIVIDENDS, side=Side.CREDIT, amount=dividend)
        )
    if rebate > ZERO:
        lines.append(LedgerLine(account=Account.REBATE_EXPENSE, side=Side.DEBIT, amount=rebate))
        lines.append(
            LedgerLine(account=Account.UNCLAIMED_DIVIDENDS, side=Side.CREDIT, amount=rebate)
        )
    return PostingSpec(
        txn_type=TxnType.DIVIDEND_POSTING,
        channel=Channel.ACCRUAL,
        amount=total,
        lines=tuple(lines),
    )


def build_share_transfer_out_posting(amount: Decimal) -> PostingSpec:
    """Transferor leg of a share transfer: ST- ref, INTERNAL.

    DR member.shares / CR clearing.share_transfers. The transfer posts
    as TWO transactions (OUT for the transferor, IN for the transferee)
    because ledger legs are attributed to members via
    transactions.member_id: a single netted posting would make the
    transferor's movement invisible to the ledger-reconstructed share
    basis and never credit the transferee's history. The
    clearing account nets to zero inside the one atomic transfer
    transaction that posts both legs.
    """
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.SHARE_TRANSFER_OUT,
        channel=Channel.INTERNAL,
        amount=amt,
        lines=(
            LedgerLine(account=Account.MEMBER_SHARES, side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.SHARE_TRANSFER_CLEARING, side=Side.CREDIT, amount=amt),
        ),
    )


def build_share_transfer_in_posting(amount: Decimal) -> PostingSpec:
    """Transferee leg of a share transfer: ST- ref, INTERNAL.

    DR clearing.share_transfers / CR member.shares — the mirror of
    build_share_transfer_out_posting; see there for the two-transaction
    rationale.
    """
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.SHARE_TRANSFER_IN,
        channel=Channel.INTERNAL,
        amount=amt,
        lines=(
            LedgerLine(account=Account.SHARE_TRANSFER_CLEARING, side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.MEMBER_SHARES, side=Side.CREDIT, amount=amt),
        ),
    )


def build_fee_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """Misc fee received from a member: FE- ref.

    DR cash / CR income.fees — the member pays the fee in, so only the
    cash channels (MPESA/BANK) are valid; ACCRUAL/INTERNAL are refused
    exactly like deposits (a fee is never a system accrual). The amount
    comes exclusively from tenant configuration —
    the application service resolves it server-side and no request body
    ever carries it.
    """
    amt = to_cents(amount)
    if channel not in (Channel.MPESA, Channel.BANK):
        raise ValueError(f"fees are only valid on MPESA or BANK channels, got {channel!r}")
    return PostingSpec(
        txn_type=TxnType.FEE,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=_cash_account(channel), side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.FEE_INCOME, side=Side.CREDIT, amount=amt),
        ),
    )


def build_write_off_posting(amount: Decimal) -> PostingSpec:
    """Committee-approved write-off provisioning posting: WO- ref.

    DR expense.loan_writeoffs / CR loans.receivable for the written-off
    PRINCIPAL balance, INTERNAL channel (no cash moves). penalty_due is
    receivable-side loan-row bookkeeping only (the rule: penalty income is recognised on receipt),
    so writing it off zeroes the loan
    row without a ledger leg — there is no penalty receivable in the
    ledger to derecognise.

    WRITE-OFF IS NOT FORGIVENESS: this posting zeroes the
    performing receivable, but the legal claim on the member survives
    in the write-once loan_write_offs snapshot; post-write-off
    recoveries are a FUTURE explicit branch (bad-debt-recovery income posting — follow-up issue
    referencing /), and a repayment
    against a written_off loan is refused loudly today.
    """
    amt = to_cents(amount)
    return PostingSpec(
        txn_type=TxnType.LOAN_WRITE_OFF,
        channel=Channel.INTERNAL,
        amount=amt,
        lines=(
            LedgerLine(account=Account.WRITE_OFF_EXPENSE, side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.LOANS_RECEIVABLE, side=Side.CREDIT, amount=amt),
        ),
    )


def build_loan_recovery_posting(amount: Decimal, channel: Channel) -> PostingSpec:
    """Bad-debt recovery receipt against a written-off loan:
    RC- ref.

    DR cash / CR income.bad_debt_recoveries — the treatment named on
    cash recovered on a claim whose receivable was already
    derecognised by the WO- posting is recognised as recovery INCOME.
    The write-off posting is never reversed and loans.receivable is
    never resurrected (the loan stays written_off; the surviving claim
    lives in the write-once loan_write_offs snapshot). Money physically
    arrives, so only the cash channels (MPESA/BANK) are valid —
    ACCRUAL/INTERNAL are refused exactly like deposits and fees.

    The AMOUNT is the cash actually received (caller-observed, like a
    repayment) — but the CLAIM it draws down (total_written_off and the
    receipts already recorded) is resolved server-side by the
    corrections service under the write-off row lock, and a receipt can
    never exceed the outstanding claim (partial recoveries tracked against total_written_off).
    """
    amt = to_cents(amount)
    if channel not in (Channel.MPESA, Channel.BANK):
        raise ValueError(
            f"recovery receipts are only valid on MPESA or BANK channels, got {channel!r}"
        )
    return PostingSpec(
        txn_type=TxnType.LOAN_RECOVERY,
        channel=channel,
        amount=amt,
        lines=(
            LedgerLine(account=_cash_account(channel), side=Side.DEBIT, amount=amt),
            LedgerLine(account=Account.RECOVERY_INCOME, side=Side.CREDIT, amount=amt),
        ),
    )


def build_reversal_posting(original: PostingSpec) -> PostingSpec:
    """Return a reversing entry that exactly negates the original (data integrity).

    Each line's side is flipped; the amount and accounts are unchanged.
    The resulting PostingSpec is balanced by construction.
    """
    reversed_lines = tuple(
        LedgerLine(
            account=ln.account,
            side=Side.CREDIT if ln.side is Side.DEBIT else Side.DEBIT,
            amount=ln.amount,
        )
        for ln in original.lines
    )
    return PostingSpec(
        txn_type=original.txn_type,
        channel=original.channel,
        amount=original.amount,
        lines=reversed_lines,
    )


# ---------------------------------------------------------------------------
# Member-facing direction (ledger listing)
# ---------------------------------------------------------------------------

#: Direction of each transaction type from the member's perspective:
#: CREDIT = money into the member's position (deposits, share top-ups,
#: repayments received, interest earned); DEBIT = money out
#: (withdrawals, disbursements, exit settlements). A reversal carries
#: the original type with mirrored legs, so callers must flip the
#: direction when reversal_of_id is set (see member_direction).
MEMBER_DIRECTION: dict[TxnType, Side] = {
    TxnType.DEPOSIT: Side.CREDIT,
    TxnType.SHARE_TOPUP: Side.CREDIT,
    TxnType.LOAN_REPAYMENT: Side.CREDIT,
    TxnType.INTEREST_POSTING: Side.CREDIT,
    TxnType.WITHDRAWAL: Side.DEBIT,
    TxnType.LOAN_DISBURSEMENT: Side.DEBIT,
    TxnType.EXIT_SETTLEMENT: Side.DEBIT,
    # a dividend/rebate credits the member's position; a share
    # transfer posts one DEBIT-direction transaction for the transferor
    # (OUT) and one CREDIT-direction transaction for the transferee (IN).
    TxnType.DIVIDEND_POSTING: Side.CREDIT,
    TxnType.SHARE_TRANSFER_OUT: Side.DEBIT,
    TxnType.SHARE_TRANSFER_IN: Side.CREDIT,
    # a fee is a charge on the member (money out of their
    # pocket); a write-off extinguishes their receivable like a
    # repayment leg would (money into their position).
    TxnType.FEE: Side.DEBIT,
    TxnType.LOAN_WRITE_OFF: Side.CREDIT,
    # Issue: a recovery receipt draws down the member's surviving
    # written-off claim — money into their position, like a repayment.
    TxnType.LOAN_RECOVERY: Side.CREDIT,
}


def member_direction(txn_type: TxnType, *, is_reversal: bool = False) -> Side:
    """DR/CR pill for the prototype ledger columns (member perspective)."""
    base = MEMBER_DIRECTION[txn_type]
    if not is_reversal:
        return base
    return Side.CREDIT if base is Side.DEBIT else Side.DEBIT


# ---------------------------------------------------------------------------
# Member-initiated activity classification (dormancy)
# ---------------------------------------------------------------------------

#: Code-owned classification of EVERY transaction type: does this type
#: represent activity the MEMBER initiated? Only member-initiated
#: activity resets the dormancy clock (the universal bank
#: bug is counting system postings, so no account ever goes dormant).
#: The completeness test pins this map to the TxnType enum, so a new
#: transaction type cannot land unclassified; the dormancy tests pin
#: the exact allow-list, so widening it is a failing diff.
#:
#:   * deposit / withdrawal / share_topup — the member moved money.
#:   * loan_repayment — the member serviced their loan.
#:   * loan_disbursement — the member borrowed (only reachable while
#:     strictly active, and the ensuing repayments keep the clock
#:     honest either way).
#:   * share_transfer_out — the transferor's own instruction.
#:   * exit_settlement — the member's own exit request (moot for
#:     dormancy: the member is terminal-exited in the same
#:     transaction).
#:   * interest_posting — SYSTEM accrual (INT- postings): never
#:     member activity.
#:   * dividend_posting — SYSTEM distribution (DV- postings, with
#:     occurred_at pinned to FY END, potentially far in the past):
#:     never member activity.
#:   * share_transfer_in — the TRANSFEREE did not act; counting it
#:     would let a third party keep an account out of dormancy
#:     monitoring.
#:
#: Reversal rows (reversal_of_id set) are staff corrections and never
#: count regardless of type — the dormancy scan excludes them with an
#: explicit predicate (application/dormancy.py).
MEMBER_INITIATED: dict[TxnType, bool] = {
    TxnType.DEPOSIT: True,
    TxnType.WITHDRAWAL: True,
    TxnType.SHARE_TOPUP: True,
    TxnType.LOAN_DISBURSEMENT: True,
    TxnType.LOAN_REPAYMENT: True,
    TxnType.INTEREST_POSTING: False,
    TxnType.EXIT_SETTLEMENT: True,
    TxnType.DIVIDEND_POSTING: False,
    TxnType.SHARE_TRANSFER_OUT: True,
    TxnType.SHARE_TRANSFER_IN: False,
    # fees are STAFF-charged (the member did not act) and a
    # write-off is a committee action — neither may reset the
    # dormancy clock.
    TxnType.FEE: False,
    TxnType.LOAN_WRITE_OFF: False,
    # Issue: recovery receipts are COLLECTIONS outcomes recorded by
    # staff — the funds may originate from auctions, guarantor calls or
    # negotiated settlements, so a receipt is never proof the member
    # transacted; counting it would quietly keep a written-off member's
    # account out of dormancy monitoring (the gaming vector).
    TxnType.LOAN_RECOVERY: False,
}


def member_initiated_types() -> tuple[str, ...]:
    """The sorted member-initiated type values (for bound SQL parameters)."""
    return tuple(sorted(t.value for t, member in MEMBER_INITIATED.items() if member))


# ---------------------------------------------------------------------------
# Account classification (income statement / SASRA return)
# ---------------------------------------------------------------------------


class AccountClass(enum.StrEnum):
    """Financial-statement class of a ledger account (code-owned).

    DEBIT-normal classes (balance = debits - credits): ASSET, EXPENSE,
    CLEARING (clearing/suspense accounts net to zero inside their
    flows, reported debit-normal so a non-zero residue is visible).
    CREDIT-normal classes (balance = credits - debits): LIABILITY,
    EQUITY, INCOME.
    """

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"
    CLEARING = "clearing"


#: Code-owned classification of EVERY chart account (the
#: account identifiers interpolated into report groupings come from
#: THIS mapping, never from callers). The completeness test pins this
#: map to the Account enum, so a new account cannot land unclassified
#: — and the report builders FAIL LOUDLY (UnprocessableError)
#: on any ledger account string not covered here, so a future account
#: can never silently vanish from an income statement or a regulator
#: return (the classic silent-drop return defect).
ACCOUNT_CLASS: dict[Account, AccountClass] = {
    Account.CASH_MPESA: AccountClass.ASSET,
    Account.CASH_BANK: AccountClass.ASSET,
    Account.LOANS_RECEIVABLE: AccountClass.ASSET,
    Account.INTEREST_RECEIVABLE: AccountClass.ASSET,
    Account.MEMBER_DEPOSITS: AccountClass.LIABILITY,
    Account.UNCLAIMED_DIVIDENDS: AccountClass.LIABILITY,
    Account.MEMBER_SHARES: AccountClass.EQUITY,
    Account.INTEREST_INCOME: AccountClass.INCOME,
    Account.PENALTY_INCOME: AccountClass.INCOME,
    Account.FEE_INCOME: AccountClass.INCOME,
    Account.RECOVERY_INCOME: AccountClass.INCOME,
    Account.INTEREST_EXPENSE: AccountClass.EXPENSE,
    Account.DIVIDEND_EXPENSE: AccountClass.EXPENSE,
    Account.REBATE_EXPENSE: AccountClass.EXPENSE,
    Account.WRITE_OFF_EXPENSE: AccountClass.EXPENSE,
    Account.SHARE_TRANSFER_CLEARING: AccountClass.CLEARING,
    Account.SUSPENSE: AccountClass.CLEARING,
}

#: Classes whose accounts carry a DEBIT-normal balance.
DEBIT_NORMAL_CLASSES: frozenset[AccountClass] = frozenset(
    {AccountClass.ASSET, AccountClass.EXPENSE, AccountClass.CLEARING}
)


def account_class(account_value: str) -> AccountClass:
    """Classify a raw ledger account string; unknown accounts RAISE.

    ValueError (unknown enum value) or KeyError (enum member missing
    from ACCOUNT_CLASS) both surface as ValueError so report builders
    can translate the failure into a loud, sanitized domain error —
    never a silently dropped line (data integrity).
    """
    try:
        return ACCOUNT_CLASS[Account(account_value)]
    except KeyError as exc:  # pragma: no cover - the completeness pin
        raise ValueError(f"ledger account {account_value!r} has no class mapping") from exc
    except ValueError:
        raise ValueError(f"unknown ledger account {account_value!r}") from None


def signed_balance(account_value: str, debits: Decimal, credits: Decimal) -> Decimal:
    """Natural-sign balance of one account (debit-normal positive for
    asset/expense/clearing; credit-normal positive for liability/
    equity/income). Raises ValueError on an unmapped account."""
    if account_class(account_value) in DEBIT_NORMAL_CLASSES:
        return debits - credits
    return credits - debits
