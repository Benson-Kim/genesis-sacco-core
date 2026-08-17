"""SASRA return skeleton: versioned, code-owned line-item mapping (P13.10).

Zero I/O — no imports from application, infrastructure, or api layers.

The regulator return maps the trial balance onto SASRA line items. The
mapping is VERSIONED per return format and lives exclusively in this
module (v1.1 rules 1/6): callers can never supply, extend, or override
line-item mappings, and every identifier interpolated into a report
comes from this code-owned table.

Coverage discipline (the classic regulator-return defect is a ledger
account silently missing from a return):

  * ``SASRA_LINES`` must PARTITION the chart of accounts — every
    ``Account`` enum member appears in exactly one line. The domain
    test pins this, so adding a chart account without deciding its
    return line is a failing diff.
  * At render time the report builder additionally fails LOUDLY on any
    ledger account string that is not covered (a raw account that
    bypassed the enum), via ``genesis.domain.ledger.account_class`` /
    ``line_for_account`` — never a silent drop.

Line amounts use the natural-sign convention from
``genesis.domain.ledger.signed_balance``: debit-normal positive for
asset/expense/clearing lines, credit-normal positive for liability/
equity/income lines. Because every posting is balanced (gate 1.5), the
double-entry identity holds on natural-sign balances — assets +
expenses + clearing == liabilities + equity + income — rendered as the
final ``CHK1`` control row (debit-normal minus credit-normal), which a
correct return shows as 0.00.
"""

from __future__ import annotations

from dataclasses import dataclass

from genesis.domain.ledger import Account

#: Return-format version tag, rendered on every row of the export so a
#: filed return is traceable to the exact mapping that produced it.
#: Bump this (and keep the old mapping importable in history) whenever
#: the regulator's prescribed line items change.
SASRA_RETURN_VERSION = "SASRA-DS-2025.1"


@dataclass(frozen=True)
class SasraLine:
    """One regulator line item and the chart accounts that feed it."""

    code: str
    title: str
    accounts: tuple[Account, ...]


#: The skeleton return, in filing order. Codes group by statement
#: side: A* assets, L* liabilities, E* equity, I* income, X* expenses,
#: C* clearing/suspense (must net to zero in a healthy book).
SASRA_LINES: tuple[SasraLine, ...] = (
    SasraLine("A100", "Cash and cash equivalents", (Account.CASH_MPESA, Account.CASH_BANK)),
    SasraLine("A200", "Gross loan portfolio", (Account.LOANS_RECEIVABLE,)),
    SasraLine("A300", "Interest receivable on loans", (Account.INTEREST_RECEIVABLE,)),
    SasraLine("L100", "Member deposits", (Account.MEMBER_DEPOSITS,)),
    SasraLine("L200", "Unclaimed dividends payable", (Account.UNCLAIMED_DIVIDENDS,)),
    SasraLine("E100", "Member share capital", (Account.MEMBER_SHARES,)),
    SasraLine("I100", "Interest income on loans", (Account.INTEREST_INCOME,)),
    SasraLine("I200", "Penalty income", (Account.PENALTY_INCOME,)),
    SasraLine("I300", "Fee income", (Account.FEE_INCOME,)),
    SasraLine("X100", "Interest expense on member deposits", (Account.INTEREST_EXPENSE,)),
    SasraLine("X200", "Dividends on member shares", (Account.DIVIDEND_EXPENSE,)),
    SasraLine("X300", "Rebates on member deposits", (Account.REBATE_EXPENSE,)),
    SasraLine(
        "C100",
        "Clearing and suspense (nets to zero)",
        (Account.SHARE_TRANSFER_CLEARING, Account.SUSPENSE),
    ),
)

#: account -> line code, derived once from SASRA_LINES. The domain test
#: pins that this is a PARTITION of the Account enum (every account in
#: exactly one line).
LINE_BY_ACCOUNT: dict[Account, str] = {
    account: line.code for line in SASRA_LINES for account in line.accounts
}


def line_for_account(account_value: str) -> str:
    """Return line code for a raw ledger account string; RAISES on any
    account the versioned mapping does not cover (fail loudly — a
    future account must never vanish from a filed return)."""
    try:
        return LINE_BY_ACCOUNT[Account(account_value)]
    except KeyError as exc:  # pragma: no cover - the partition pin
        raise ValueError(f"ledger account {account_value!r} has no SASRA line") from exc
    except ValueError:
        raise ValueError(
            f"unknown ledger account {account_value!r} is not covered by "
            f"the {SASRA_RETURN_VERSION} mapping"
        ) from None
