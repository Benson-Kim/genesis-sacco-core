"""members numeric member_no keyset index

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-07

Expand-only revision backing the post-merge remediation of senior
review (posted on): the tenant-wide members listing
(application/members.py MEMBER_LIST_SQL and the aggregates variant
MEMBER_LIST_AGGREGATES_SQL) now orders and keyset-paginates in NUMERIC
member_no order — ORDER BY (length(member_no), member_no) — because
the domain format ('GP-' + zero-padded sequence of AT LEAST four
digits) is monotonic numerically but NOT lexicographically: as bare
TEXT 'GP-10000' < 'GP-9999', so past 9,999 members the old order was
wrong and its keyset walk skipped/duplicated rows.

Index audit first (v1.1 discipline): members carries the
(tenant_id, member_no) UNIQUE key (0001), which serves the OLD
lexicographic order but CANNOT serve ORDER BY length(member_no),
member_no — without this index every listing page becomes a top-N
sort over the whole tenant slice, defeating keyset pagination (gate
1.3: every ORDER BY expression of a production query is indexed). The
expression index is therefore genuinely required and declared (rule
14, claimed in the MR description at branch time):

  * idx_members_member_no_numeric — (tenant_id, length(member_no),
    member_no): the tenant-scoped numeric keyset. The row-value
    predicate (length(member_no), member_no) > (length(cursor),
    :cursor) walks a contiguous suffix of the index columns, so every
    page is one index scan with NO Sort node (EXPLAIN-asserted in
    tests/test_member_no_numeric_order.py with enable_sort=off;
    falsifiable by dropping this index). length() over text is
    IMMUTABLE, so the expression is indexable as-is.

The equality-probed single-branch roster
(branches.py:branch_members_roster_sql) stays a bounded top-N under
idx_members_branch (0016) and deliberately gets NO new index.

NO table, column, constraint or RLS change: one index only, shipped
in the SAME MR as the queries it serves. Downgrade drops the index
exactly (reads only — the loud-refusal discipline is not implicated).

RENUMBERED 0040 -> 0041 and re-chained onto 0040 after the
merge (0040_share_transfer_maker_checker.py) claimed revision 0040 on
main while this MR was in flight (the migration-declaration rule; the 0017 re-chain precedent —
never renumber another track's claim: the later-merging track takes the next free number).
"""

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

_UP = """
CREATE INDEX idx_members_member_no_numeric
    ON members (tenant_id, length(member_no), member_no);
"""

_DOWN = """
DROP INDEX idx_members_member_no_numeric;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
