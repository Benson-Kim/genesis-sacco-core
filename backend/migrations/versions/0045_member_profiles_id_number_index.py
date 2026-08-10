"""member KYC id-number lookup expression index (issue #35 item 14 residual)

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-09

Expand-only revision completing the posting-drawer unique-identifier
lookup story (#35 item 14): the member_no exact-match probe shipped in
the remainder round over the 0001 UNIQUE key; the ID-NUMBER probe now
resolves a member by the person profile's national ID — an exact match
on the KYC jsonb expression (profile -> 'bio' ->> 'id_number'),
executed by application/members.py MEMBER_ID_NUMBER_LOOKUP_SQL (bound
parameter, explicit tenant predicate on top of forced RLS).

Index audit first (v1.1 discipline): member_profiles carries only the
0018 UNIQUE (tenant_id, member_id) claim key — nothing covers the
jsonb expression, so the new hot-path equality probe would seq-scan
the tenant slice. The index is therefore genuinely required, shipped
in the SAME MR as the query it serves (the house rule: refuse to ship
the probe without the index), and declared (rule 14, claimed in the
MR description at branch time; head 0044 and every open MR re-checked
via the API at claim time):

  * idx_member_profiles_id_number —
    (tenant_id, (profile -> 'bio' ->> 'id_number'))
    WHERE (profile -> 'bio' ->> 'id_number') IS NOT NULL:
    tenant-leading so the RLS qual and the id-number equality are both
    index-served in one probe; partial because only PERSON profiles
    carry a bio.id_number (company/group/vehicle profiles have no
    'bio' key, so the expression is NULL for them and they can never
    match a national-ID lookup). EXPLAIN structural gate in
    tests/test_idnumber_lookup.py (zero Seq Scan and zero Sort nodes
    with enable_seqscan=off; falsifiable by dropping this index).

NO table, column, constraint or RLS change: one partial expression
index only. No UNIQUE claim: national IDs are attested KYC data, not
a platform-issued identity — a duplicate entry is an operator-visible
data-quality fact the lookup surfaces (multiple rows), never a silent
constraint casualty on legacy rows. Downgrade drops the index exactly
(reads only — the loud-refusal discipline is not implicated).
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_UP = """
CREATE INDEX idx_member_profiles_id_number
    ON member_profiles (tenant_id, (profile -> 'bio' ->> 'id_number'))
 WHERE (profile -> 'bio' ->> 'id_number') IS NOT NULL;
"""

_DOWN = """
DROP INDEX idx_member_profiles_id_number;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
