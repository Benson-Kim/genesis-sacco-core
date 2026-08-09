"""Domain pins for the P13.10 account classification & SASRA mapping.

Pure tests (no DB). These are the compile-time half of the fail-loud
coverage discipline: the render-time half (an unmapped RAW account
string aborts the export job) lives in test_income_statement_export /
test_sasra_return_export. Every pin is falsifiable — remove an entry
from ACCOUNT_CLASS or SASRA_LINES (or add a chart account without
deciding its class/line) and the corresponding pin fails.
"""

from decimal import Decimal

import pytest

from genesis.domain.ledger import (
    ACCOUNT_CLASS,
    DEBIT_NORMAL_CLASSES,
    Account,
    AccountClass,
    account_class,
    signed_balance,
)
from genesis.domain.sasra import (
    LINE_BY_ACCOUNT,
    SASRA_LINES,
    SASRA_RETURN_VERSION,
    line_for_account,
)


def test_account_class_map_covers_every_chart_account_exactly() -> None:
    """A new Account enum member cannot land unclassified: the map's
    key set must EQUAL the enum (gate 1.5 — the silent-drop defect)."""
    assert set(ACCOUNT_CLASS) == set(Account)


def test_account_class_side_conventions_are_pinned() -> None:
    """Hand-derived accounting conventions, pinned account by account
    so a re-classification is always a reviewed diff."""
    assert account_class(Account.CASH_MPESA.value) is AccountClass.ASSET
    assert account_class(Account.CASH_BANK.value) is AccountClass.ASSET
    assert account_class(Account.LOANS_RECEIVABLE.value) is AccountClass.ASSET
    assert account_class(Account.INTEREST_RECEIVABLE.value) is AccountClass.ASSET
    assert account_class(Account.MEMBER_DEPOSITS.value) is AccountClass.LIABILITY
    # The !36 payable: recognised dividends owed to mid-run leavers.
    assert account_class(Account.UNCLAIMED_DIVIDENDS.value) is AccountClass.LIABILITY
    assert account_class(Account.MEMBER_SHARES.value) is AccountClass.EQUITY
    assert account_class(Account.INTEREST_INCOME.value) is AccountClass.INCOME
    assert account_class(Account.PENALTY_INCOME.value) is AccountClass.INCOME
    assert account_class(Account.FEE_INCOME.value) is AccountClass.INCOME
    assert account_class(Account.INTEREST_EXPENSE.value) is AccountClass.EXPENSE
    assert account_class(Account.DIVIDEND_EXPENSE.value) is AccountClass.EXPENSE
    assert account_class(Account.REBATE_EXPENSE.value) is AccountClass.EXPENSE
    assert account_class(Account.SHARE_TRANSFER_CLEARING.value) is AccountClass.CLEARING
    assert account_class(Account.SUSPENSE.value) is AccountClass.CLEARING
    assert {
        AccountClass.ASSET,
        AccountClass.EXPENSE,
        AccountClass.CLEARING,
    } == DEBIT_NORMAL_CLASSES


def test_unknown_account_string_raises_never_drops() -> None:
    """The render-time fail-loud guard (H1): an account outside the
    code-owned chart raises instead of silently vanishing."""
    with pytest.raises(ValueError, match=r"zz\.novel"):
        account_class("zz.novel")
    with pytest.raises(ValueError, match=r"zz\.novel"):
        line_for_account("zz.novel")
    with pytest.raises(ValueError, match=r"zz\.novel"):
        signed_balance("zz.novel", Decimal("1.00"), Decimal("0.00"))


def test_signed_balance_conventions() -> None:
    """Hand-computed: debit-normal (asset) 10 DR / 4 CR -> +6;
    credit-normal (liability) 4 DR / 10 CR -> +6; income credit-normal;
    expense debit-normal."""
    assert signed_balance("cash.bank", Decimal("10.00"), Decimal("4.00")) == Decimal("6.00")
    assert signed_balance(
        "liability.unclaimed_dividends", Decimal("4.00"), Decimal("10.00")
    ) == Decimal("6.00")
    assert signed_balance("income.interest", Decimal("0.00"), Decimal("7.50")) == Decimal("7.50")
    assert signed_balance("expense.dividends", Decimal("7.50"), Decimal("0.00")) == Decimal("7.50")


def test_sasra_lines_partition_the_chart_of_accounts() -> None:
    """Every chart account appears in EXACTLY ONE return line — a
    partition, not mere coverage (an account in two lines would be
    double-counted in a filed return; an account in none would
    vanish). Falsifiable: drop UNCLAIMED_DIVIDENDS from L200 and the
    set equality fails; list it on a second line and the multiset
    length check fails."""
    listed = [account for line in SASRA_LINES for account in line.accounts]
    assert len(listed) == len(set(listed))  # no account on two lines
    assert set(listed) == set(Account)  # no account off the return
    assert set(LINE_BY_ACCOUNT) == set(Account)


def test_sasra_line_codes_are_unique_and_version_is_pinned() -> None:
    codes = [line.code for line in SASRA_LINES]
    assert len(codes) == len(set(codes))
    # The version tag every rendered row carries; bumping the mapping
    # without bumping the version is a rejected diff (v1.1 rules 1/6).
    # 2025.2: P13.15 added X400 (loan write-off expense) — the mapping
    # changed, so the version moved with it. 2025.3: issue #21 added
    # I400 (recoveries on loans written off) — same rule.
    assert SASRA_RETURN_VERSION == "SASRA-DS-2025.3"
    # The !36 payable has its own regulator line.
    assert line_for_account(Account.UNCLAIMED_DIVIDENDS.value) == "L200"
    # P13.15: the write-off expense has its own regulator line.
    assert line_for_account(Account.WRITE_OFF_EXPENSE.value) == "X400"
    # Issue #21: recovery income has its own regulator line.
    assert line_for_account(Account.RECOVERY_INCOME.value) == "I400"
