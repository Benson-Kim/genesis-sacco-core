"""merge the 0046 / 0047 / 0048 parallel-track heads

Revision ID: 0049
Revises: 0046, 0047, 0048
Create Date: 2026-08-18

Three parallel tracks branched the migration chain from 0044: the KYC
phone-E.164 line (0045 -> 0046), the transactions-ledger export check
widening (0047) and the human loan/exit references (0048). Each head
is internally consistent and they touch disjoint objects, but Alembic
refuses `upgrade head` while multiple heads exist — every MR pipeline's
backend:test dies at the upgrade step before a single test runs.

This is the standard Alembic MERGE revision: schema NO-OP in both
directions (upgrade/downgrade change nothing); its only effect is to
give the DAG a single head again. migrate-check's downgrade -1 walks
back to the three-head state and re-upgrades — both are no-ops here.

Index audit (rule 14): no schema change, no new statement, no index.
"""

revision = "0049"
down_revision = ("0046", "0047", "0048")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: merge point only."""


def downgrade() -> None:
    """No-op: re-opens the three-head state exactly."""
