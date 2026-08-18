"""merge the 0046 / 0047 / 0048 heads left by parallel merges

Revision ID: 0049
Revises: 0046, 0047, 0048
Create Date: 2026-08-18

Develop carried THREE alembic heads (0044 → 0045 → 0046, 0044 → 0047,
0044 → 0048): the branches merged in Git without a migration merge
point, and — because develop-branch pushes ran no pipeline until the
security-hardening MR added them — `alembic upgrade head` failed with
"Multiple head revisions" on the first migrated environment to notice.
This is a MERGE-ONLY revision: no schema change in either direction.
The three parents touch disjoint objects (0046: member_profiles phone
backfill; 0047: exports report CHECK; 0048: loans/member_exits human
refs), so no ordering conflict exists between them.
"""

revision = "0049"
down_revision = ("0046", "0047", "0048")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge point only — nothing to apply."""


def downgrade() -> None:
    """Merge point only — nothing to revert."""
