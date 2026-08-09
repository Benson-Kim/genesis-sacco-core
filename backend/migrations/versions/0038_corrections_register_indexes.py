"""corrections register keyset indexes

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-06

Expand-only revision backing the HUMAN-AUTHORIZED
read-contract expansion (ledger items (a).1 and (a).2): the
corrections contract gains keyset LIST reads — the pending-adjustments
checker register and the write-off register — so the checker/committee
no longer works from a hand-carried id (the console shipped by-id only because no list endpoint
existed; the gap was recorded on and is closed here). NO table, column, constraint or RLS change:
two indexes only, shipped in the SAME MR as the queries they serve
(scalability — index ships with its query).

Index audit first (v1.1 discipline): repayment_adjustments carries
(tenant_id, loan_id/orig_txn/rev_txn/maker/checker) FK indexes and the
partial uq claim (0025/0031); loan_write_offs carries (tenant_id,
loan_id/member_id/transaction_id) and the partial uq slot (0025).
NONE serves a register-ordered keyset walk, so the indexes are
genuinely required and declared:

  * idx_repayment_adjustments_register — (tenant_id,
    (status = 'pending_approval') DESC, created_at DESC, id DESC):
    the checker's job order — PENDING FIRST, newest first inside each
    band. The boolean expression is the register's two-band rank; the
    uniform-DESC keyset row comparison in
    application/corrections.py:adjustments_register_sql walks it
    exactly (EXPLAIN-asserted; falsifiable by dropping the index).

  * idx_loan_write_offs_register — (tenant_id,
    (status IN ('requested', 'approved')) DESC, created_at DESC,
    id DESC): the committee's job order — LIVE (requested/approved)
    first; terminal history (rejected/posted) behind. Same keyset
    shape (write_offs_register_sql).

Both expressions are IMMUTABLE text comparisons over the status CHECK
vocabulary pinned by 0025/0031 — a status vocabulary change already
requires a successor migration (the 0026 named-contract discipline),
which must regenerate these expressions in the same MR.

Downgrade drops the two indexes exactly (no data is touched — reads
only; the loud-refusal discipline is not implicated).
"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_UP = """
CREATE INDEX idx_repayment_adjustments_register
    ON repayment_adjustments
        (tenant_id, (status = 'pending_approval') DESC,
         created_at DESC, id DESC);

CREATE INDEX idx_loan_write_offs_register
    ON loan_write_offs
        (tenant_id, (status IN ('requested', 'approved')) DESC,
         created_at DESC, id DESC);
"""

_DOWN = """
DROP INDEX idx_loan_write_offs_register;
DROP INDEX idx_repayment_adjustments_register;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
