"""members.dividend_payout — stored payout PREFERENCE

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-06

Expand-only revision for ledger item (c), claimed as
0039 with down_revision '0038' in the MR description at branch time
(the migration-declaration rule; head 0038 re-verified against main, merge eeb8113).

One nullable TEXT column on members carrying the member's dividend
payout PREFERENCE. The vocabulary is CODE-OWNED (domain/members.py
DividendPayout — see its provenance note: the prototype declares the
field but no option list; the values are the prototype's own cash
channels plus its two retention destinations) and is enforced AT THE
DATABASE by the CHECK below (data integrity: constraints in the database, not just the app).

NULL is the honest "not chosen" state: every existing member keeps
NULL — there is deliberately NO backfill and NO default (an invented
default would fabricate a preference no member ever expressed).

REGULATORY/ACCOUNTING FENCE: this column is a stored
preference ONLY. The dividends distribution engine does NOT
read it in this batch — routing money by preference is a separate,
separately-authorized batch. No money moves; no ledger contact.

Downgrade drops the column (expand -> migrate -> contract: the column
is nullable, unconsumed by any money path, and never backfilled, so
the drop loses only explicitly-stored preferences — no loud-refusal
guard is owed, unlike identity/consent history in 0035).
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE members
    ADD COLUMN dividend_payout text
        CONSTRAINT ck_members_dividend_payout CHECK (
            dividend_payout IS NULL OR dividend_payout IN (
                'deposit_account', 'share_capital', 'mpesa', 'bank'
            )
        );
"""

_DOWN = """
ALTER TABLE members DROP COLUMN IF EXISTS dividend_payout;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
