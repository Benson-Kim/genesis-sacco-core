"""members/users phone local-format -> E.164 backfill (item 1)

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-08

Expand-only DATA backfill (the migration-declaration rule, claimed in the MR description at branch
time): the maintainer-declared canonical storage for Kenya
mobile identifiers is E.164 (+254…). The write path now normalizes
every accepted spelling (07XXXXXXXX / 01XXXXXXXX / +2547XXXXXXXX /
+2541XXXXXXXX) via domain/members.normalize_kenya_msisdn; this
revision migrates the rows that predate it.

Scope is deliberately EXACT: only values matching the full local
format ^0[71][0-9]{8}$ are rewritten (anchored — no digit harvesting
from free text; a value in any other shape is left untouched and
read-tolerated, matching the application layer's legacy tolerance).
The rewrite is a pure prefix substitution 0X… -> +254X…, so it is
losslessly invertible.

NO schema, constraint, index or RLS change: two bounded UPDATEs on
small operational tables (members, users), safe in one transaction.
Old code reads E.164 rows fine (phone is free text to it), so the
change is backward-compatible one release.

Downgrade is the EXACT inverse: rows matching ^\\+254[71][0-9]{8}$ are
rewritten back to the local 0X… form. This restores the pre-upgrade
representation for every row the upgrade rewrote; an E.164 row written
natively by the new code is also rewritten, which remains a valid
accepted input format for the new write path (documented trade-off —
the two populations are indistinguishable by construction).
"""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

_UP = """
UPDATE members
   SET phone = '+254' || substr(phone, 2)
 WHERE phone ~ '^0[71][0-9]{8}$';

UPDATE users
   SET phone = '+254' || substr(phone, 2)
 WHERE phone ~ '^0[71][0-9]{8}$';
"""

_DOWN = """
UPDATE members
   SET phone = '0' || substr(phone, 5)
 WHERE phone ~ '^\\+254[71][0-9]{8}$';

UPDATE users
   SET phone = '0' || substr(phone, 5)
 WHERE phone ~ '^\\+254[71][0-9]{8}$';
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
