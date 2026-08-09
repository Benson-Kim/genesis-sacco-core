"""users phone sign-in lookup index (issue #35 sign-in identifier)

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-08

Expand-only revision for the maintainer-authorized sign-in-identifier
round (#35): staff OTP sign-in now accepts ONE identifier that may be
email OR phone. A phone-shaped identifier is normalized through the
single existing normalizer (domain/members.py:normalize_kenya_msisdn —
the E.164 round, 0042 backfilled storage) and resolved against stored
E.164 users.phone (application/auth.py USER_BY_PHONE_*_SQL, bound
parameter).

Index audit first (v1.1 discipline): users carries UNIQUE
(tenant_id, email) (0001) serving the email path, idx_users_role
(0001), the 0015 keyset index and the 0016 branch indexes — NOTHING
leads with or covers phone, so the new hot-path equality probe
WHERE phone = :identifier (executed under forced RLS, whose policy
qual supplies tenant_id = current_setting equality) would seq-scan
the tenant slice. The index is therefore genuinely required and
declared (rule 14, claimed in the MR description at branch time):

  * idx_users_phone — (tenant_id, phone) WHERE phone IS NOT NULL:
    tenant-leading so the RLS qual and the phone equality are both
    index-served in one probe; partial because NULL-phone rows can
    never match a sign-in identifier (normalize_kenya_msisdn never
    returns NULL for an accepted input). EXPLAIN structural gate in
    tests/test_signin_identifier.py (zero Seq Scan nodes with
    enable_seqscan=off; falsifiable by dropping this index).

NO table, column, constraint or RLS change: one partial index only,
shipped in the SAME MR as the query it serves. No UNIQUE claim: phone
is deliberately NOT unique at the schema level (pre-E.164 legacy rows
and shared branch handsets exist in the wild); the sign-in resolution
takes the same first-match posture as the email path's LIMIT-less
.first() and never discloses multiplicity. Downgrade drops the index
exactly (reads only — the loud-refusal discipline is not implicated).
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_UP = """
CREATE INDEX idx_users_phone
    ON users (tenant_id, phone)
 WHERE phone IS NOT NULL;
"""

_DOWN = """
DROP INDEX idx_users_phone;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
