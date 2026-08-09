"""external transaction reference + ledger search prefix index (#35 items 6, 13)

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-08

Expand-only revision for the #35 remainder round (MR-declared per rule
14; authored with down_revision = '0041' — main's head at branch time;
re-chained onto '0042' after the in-flight !87 claim
(0042_phone_e164_backfill.py) merged to main — another track's number,
never renumbered here; the 0017/0041 precedent).

Item 6 — mandatory external transaction reference:

  * transactions.external_ref — NULLABLE text, CHECK bounded 2..40
    chars. The operator-entered external money reference (M-Pesa
    confirmation code / bank slip ref) for the EXTERNAL channels
    (mpesa, bank — the only channels the teller boundary accepts;
    accrual/internal are server-job postings that keep their
    system-generated txn_ref only). NULL is the honest legacy state:
    every pre-0043 row and every system posting stays NULL — history
    is never backfilled with invented references.
  * uq_txns_external_ref — partial UNIQUE (tenant_id, channel,
    external_ref) WHERE external_ref IS NOT NULL: per-tenant
    per-channel dedupe of external references (a re-keyed M-Pesa code
    is a duplicate-money-in incident, 409 at the atomic INSERT claim —
    never SELECT-then-INSERT); legacy NULLs tolerated by the partial
    predicate.

Item 13 (search leg) — index audit first (v1.1 discipline): the
GET /transactions search predicate probes txn_ref by PREFIX
(txn_ref LIKE :prefix ESCAPE '\\'). The existing 0001 UNIQUE
(tenant_id, txn_ref) btree serves that prefix probe ONLY under the C
collation; under any ICU/libc locale a LIKE prefix needs
text_pattern_ops, so the probe is portable-index-served by:

  * idx_txns_ref_prefix — (tenant_id, txn_ref text_pattern_ops).

The member-match branch of the same predicate is an EXISTS probing
members by PRIMARY KEY per candidate row (id = transactions.member_id)
and deliberately gets NO new index. The EXPLAIN structural gate
(tests/test_txn_search.py) proves the production search page is
index-served with no Seq Scan and no Sort node.

NO RLS change (transactions already carries the forced-RLS tenant
policy; the new column inherits it). Downgrade drops the two indexes
and the column exactly (the column is new — no pre-existing data can
be lost; refs recorded meanwhile also live in the in-transaction audit
rows, which the downgrade never touches).
"""

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE transactions
    ADD COLUMN external_ref text
        CHECK (external_ref IS NULL
               OR char_length(external_ref) BETWEEN 2 AND 40);

CREATE UNIQUE INDEX uq_txns_external_ref
    ON transactions (tenant_id, channel, external_ref)
    WHERE external_ref IS NOT NULL;

CREATE INDEX idx_txns_ref_prefix
    ON transactions (tenant_id, txn_ref text_pattern_ops);
"""

_DOWN = """
DROP INDEX idx_txns_ref_prefix;

DROP INDEX uq_txns_external_ref;

ALTER TABLE transactions DROP COLUMN external_ref;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
