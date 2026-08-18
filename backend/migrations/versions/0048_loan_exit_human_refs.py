"""per-tenant human reference numbers for loans and member exits (#35)

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-09

Re-chained from 0044 to 0047 after the sibling MRs merged (the 0017
re-chain discipline): 0044 briefly had three children (0045, 0047,
0048), which breaks `alembic upgrade head` with multiple heads. The
revision is independent of 0045-0047 content, so only the declared
parent changes.

The loan-book and exit-statement exports printed raw row UUIDs because
no human reference existed in the schema (the identifier-doctrine
residual). This revision gives loans and member_exits a per-tenant
human reference (LN-0001 / EX-0001 style, the member-number format
discipline):

  * EXPAND-ONLY nullable columns loans.loan_ref / member_exits.exit_ref
    (no NOT NULL — one release of backward compatibility; old code
    ignores the column, new code always writes it).
  * Per-tenant UNIQUE partial indexes (tenant_id, ref) WHERE ref IS
    NOT NULL — the final safety net under the race-safe allocator
    (application/ledger.py:allocate_sequence — the P7 advisory-lock +
    txn_ref_sequences pattern, the SAME mechanism member numbering
    uses).
  * DETERMINISTIC backfill in (created_at, id) order per tenant —
    row_number() over that total order, so the assigned references are
    hand-computable from the seeded rows and re-runs assign nothing
    (WHERE ref IS NULL predicate keeps it idempotent).
  * Counter seeding: txn_ref_sequences rows for the 'loan_ref' /
    'exit_ref' sequence keys are raised to the backfilled count per
    tenant (GREATEST keeps an already-larger counter), so the next
    live allocation continues after the backfill instead of colliding
    into the UNIQUE net.
  * EXACT downgrade: drop the two indexes and columns, delete the two
    sequence keys — byte-for-byte pre-revision schema (migrate-check
    walks upgrade -> downgrade -> upgrade over the full chain).

Index audit (rule 14): the two partial UNIQUE indexes are the only new
indexes and they ship WITH the uniqueness guarantee that needs them;
no new hot-path predicate is introduced (reads keep their existing
keyset indexes; the ref rides already-served statements).

NOTE on the sequence keys: 'loan_ref' / 'exit_ref' are deliberately
NOT the display prefixes ('LN-' / 'EX-') — 'LN-' is already the loan
DISBURSEMENT txn-ref counter key (domain/ledger.py REF_PREFIX) and
sharing it would interleave two unrelated sequences.
"""

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

#: Expand step — columns + per-tenant UNIQUE nets. Module-level
#: constants so the backfill suite executes the EXACT production
#: statements (the 0042/0046 testability precedent).
_UP_EXPAND = """
ALTER TABLE loans ADD COLUMN loan_ref text;
ALTER TABLE member_exits ADD COLUMN exit_ref text;

CREATE UNIQUE INDEX uq_loans_loan_ref
    ON loans (tenant_id, loan_ref)
 WHERE loan_ref IS NOT NULL;

CREATE UNIQUE INDEX uq_member_exits_exit_ref
    ON member_exits (tenant_id, exit_ref)
 WHERE exit_ref IS NOT NULL;
"""

#: Deterministic backfill: (created_at, id) is a TOTAL order (id is
#: the PK tie-break), so row_number() is stable and the references are
#: hand-computable. WHERE ref IS NULL makes re-runs no-ops (idempotent
#: by row count: first run N rows, second run 0).
_UP_BACKFILL_LOANS = """
UPDATE loans l
   SET loan_ref = 'LN-' || lpad(o.rn::text, 4, '0')
  FROM (
        SELECT id, tenant_id,
               row_number() OVER (
                   PARTITION BY tenant_id ORDER BY created_at, id
               ) AS rn
          FROM loans
         WHERE loan_ref IS NULL
       ) o
 WHERE o.id = l.id
   AND o.tenant_id = l.tenant_id
   AND l.loan_ref IS NULL;
"""

_UP_BACKFILL_EXITS = """
UPDATE member_exits e
   SET exit_ref = 'EX-' || lpad(o.rn::text, 4, '0')
  FROM (
        SELECT id, tenant_id,
               row_number() OVER (
                   PARTITION BY tenant_id ORDER BY created_at, id
               ) AS rn
          FROM member_exits
         WHERE exit_ref IS NULL
       ) o
 WHERE o.id = e.id
   AND o.tenant_id = e.tenant_id
   AND e.exit_ref IS NULL;
"""

#: Counter seeding — the live allocator continues AFTER the backfill.
#: GREATEST never lowers an existing counter (idempotent under re-run).
_UP_SEED_COUNTERS = """
INSERT INTO txn_ref_sequences (tenant_id, prefix, last_val)
SELECT tenant_id, 'loan_ref', count(*) FROM loans
 WHERE loan_ref IS NOT NULL
 GROUP BY tenant_id
ON CONFLICT (tenant_id, prefix) DO UPDATE
   SET last_val = GREATEST(txn_ref_sequences.last_val, EXCLUDED.last_val);

INSERT INTO txn_ref_sequences (tenant_id, prefix, last_val)
SELECT tenant_id, 'exit_ref', count(*) FROM member_exits
 WHERE exit_ref IS NOT NULL
 GROUP BY tenant_id
ON CONFLICT (tenant_id, prefix) DO UPDATE
   SET last_val = GREATEST(txn_ref_sequences.last_val, EXCLUDED.last_val);
"""

#: Exact inverse: columns, indexes and the two sequence keys vanish.
_DOWN = """
DROP INDEX uq_loans_loan_ref;
DROP INDEX uq_member_exits_exit_ref;
ALTER TABLE loans DROP COLUMN loan_ref;
ALTER TABLE member_exits DROP COLUMN exit_ref;
DELETE FROM txn_ref_sequences WHERE prefix IN ('loan_ref', 'exit_ref');
"""


def upgrade() -> None:
    op.execute(_UP_EXPAND)
    op.execute(_UP_BACKFILL_LOANS)
    op.execute(_UP_BACKFILL_EXITS)
    op.execute(_UP_SEED_COUNTERS)


def downgrade() -> None:
    op.execute(_DOWN)
