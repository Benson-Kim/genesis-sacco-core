"""guarantee-loan linkage backfill

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-29

Data-only (backfill-safe for existing rows, no schema change):

Disbursement now links guarantees to the loan inside the atomic P7
transaction (application/ledger.py); this UPDATE is the backfill for
rows disbursed before the rule existed. Every live guarantee whose
application already produced a loan gets loan_id set; live pledges
behind an ACTIVE loan become active (the guarantee lifecycle follows
the loan from here on); live pledges behind an already-terminal loan
are released — the release-on-closure hook would have released
them at closure had the linkage existed then. idx_guarantees_application
(0001) backs the join; no new index is needed. Migrations run as the
schema owner, so RLS does not fence this backfill; the tenant equality
in the join keeps the linkage tenant-correct regardless.

Downgrade is a deliberate no-op: the backfill is a pure data narrowing
(loan_id/status corrections) — reverting it would recreate the
orphaned-guarantee bug on rows the fix already repaired.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_UP = """
UPDATE guarantees g
SET loan_id = l.id,
    status = CASE WHEN l.status = 'active' THEN 'active' ELSE 'released' END,
    version = g.version + 1,
    updated_at = now()
FROM loans l
WHERE l.application_id = g.application_id
  AND l.tenant_id = g.tenant_id
  AND g.loan_id IS NULL
  AND g.status IN ('pledged', 'active');
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Deliberate no-op (documented above): data-only backfill whose
    # reversal would reintroduce the orphaned-guarantee bug.
    pass
