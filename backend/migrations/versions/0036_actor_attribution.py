"""actor attribution: created_by on loan_applications and transactions

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-04

Additive revision backing the audit contract follow-ups
(closing-summary risk 4; Hat 1), claimed as 0036 with
down_revision '0035' in its MR description and the build plan
registry at branch time (the migration-declaration rule; head 0034 + the single in-flight 0035
claim verified — declared merge order: first, then).

  * loan_applications.created_by — the acting principal that INSERTed
    the application, recorded at creation from then on. Powers the R4
    disbursement separation-of-duties check (the initiator of a loan application cannot post its
    disbursement — the self-settle mirror) and rides the ApplicationOut read model as the bare staff
    UUID (least disclosure; resolving it stays behind access_control).

  * transactions.created_by — the acting principal of every ledger
    posting, recorded at INSERT in ledger._post (R3/Hat 2). NULL for
    system/job postings (interest accrual, dormancy sweeps) — a NULL
    actor is an honest "posted by the system", never a fabricated
    principal. The 0004 append-only triggers pin the column immutable
    the moment the row commits: attribution can never be
    rewritten after the fact.

  * Backfill — ONLY from each row's own in-transaction audit record
    (the 0031 truthful-history rule; audit rows are written in the same
    transaction as the row they describe, so they are the one authentic
    source): action='application.create' / action='ledger.post', joined
    on (tenant_id, entity, entity_id) via idx_audit_entity (0001), and
    ONLY where exactly one distinct non-NULL actor exists for the row
    (HAVING count(DISTINCT actor_id) = 1) — an ambiguous or actorless
    history stays NULL. Attribution is never invented.
    transactions is fenced append-only (0004), so its backfill runs
    between ALTER TABLE... DISABLE/ENABLE TRIGGER inside this single
    migration transaction — the fence is provably back in force at
    commit (FM-C; DDL and DML are transactional in PostgreSQL, so a failure anywhere rolls back BOTH
    the backfill and the trigger toggle). Migrations run as the schema owner, so forced RLS does not
    fence the backfill (the 0011 precedent); the tenant equality in
    each join keeps attribution tenant-correct regardless.

  * NO new indexes (v1.1 rule; EXPLAIN posture documented in): no
    read path filters or orders by created_by — the SoD check reads the
    column under the application row lock already taken by PK, the read
    models only ADD the column to their SELECT lists (existing keyset
    indexes unchanged), and FK validation on INSERT is a users-PK
    lookup. No hot path exists to serve, so no index is shipped; the
    write-heaviest table in the system (transactions) is not taxed on
    every INSERT for a column nothing scans.

  * NO RLS changes: both tables already carry forced RLS (0001);
    TENANT_TABLES and the leakage suite are unchanged.

Downgrade REFUSES LOUDLY (the 0017/0020/0035 precedent) when any
recorded attribution exists: silently dropping WHO initiated an
application or posted a ledger transaction would recreate the very
 audit gap this revision closes, as data loss. On a clean
expansion (migrate-check's up -> down -> up) it reverses exactly.
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE loan_applications
    ADD COLUMN created_by uuid REFERENCES users(id) ON DELETE RESTRICT;

ALTER TABLE transactions
    ADD COLUMN created_by uuid REFERENCES users(id) ON DELETE RESTRICT;

-- Backfill applications from their own in-transaction audit record
-- (action='application.create' is written exactly once, atomically
-- with the INSERT it describes). Only unambiguous history is copied:
-- exactly one distinct non-NULL actor, else the row stays NULL.
UPDATE loan_applications la
SET created_by = src.actor_id
FROM (
    SELECT tenant_id, entity_id, min(actor_id::text)::uuid AS actor_id
    FROM audit_log
    WHERE entity = 'loan_applications'
      AND action = 'application.create'
      AND actor_id IS NOT NULL
    GROUP BY tenant_id, entity_id
    HAVING count(DISTINCT actor_id) = 1
) src
WHERE la.tenant_id = src.tenant_id
  AND la.id::text = src.entity_id
  AND la.created_by IS NULL;

-- Backfill postings from their own in-transaction audit record
-- (action='ledger.post', written by ledger._post in the posting
-- transaction). The 0004 append-only fence must yield for this one
-- statement: the trigger is disabled and re-enabled INSIDE this
-- migration transaction, so the fence is provably back in force at
-- commit and no other writer can slip an UPDATE through (DISABLE
-- TRIGGER takes ACCESS EXCLUSIVE on the table).
ALTER TABLE transactions DISABLE TRIGGER transactions_no_update;

UPDATE transactions t
SET created_by = src.actor_id
FROM (
    SELECT tenant_id, entity_id, min(actor_id::text)::uuid AS actor_id
    FROM audit_log
    WHERE entity = 'transactions'
      AND action = 'ledger.post'
      AND actor_id IS NOT NULL
    GROUP BY tenant_id, entity_id
    HAVING count(DISTINCT actor_id) = 1
) src
WHERE t.tenant_id = src.tenant_id
  AND t.id::text = src.entity_id
  AND t.created_by IS NULL;

ALTER TABLE transactions ENABLE TRIGGER transactions_no_update;
"""

_DOWN_GUARD = """
DO $guard$
DECLARE
    attributed_applications bigint;
    attributed_transactions bigint;
BEGIN
    SELECT count(*) INTO attributed_applications
        FROM loan_applications WHERE created_by IS NOT NULL;
    SELECT count(*) INTO attributed_transactions
        FROM transactions WHERE created_by IS NOT NULL;
    IF attributed_applications > 0 OR attributed_transactions > 0 THEN
        RAISE EXCEPTION USING MESSAGE = format(
            'refusing downgrade 0036: recorded actor attribution exists '
            '(%s attributed loan applications, %s attributed ledger '
            'transactions). Dropping WHO initiated an application or '
            'posted a transaction would recreate the issue-#30 R3/R4 '
            'audit gap as data loss (the 0017/0020 refusal precedent) - '
            'attribution is never silently destroyed.',
            attributed_applications, attributed_transactions);
    END IF;
END
$guard$;
"""

_DOWN = """
ALTER TABLE transactions DROP COLUMN IF EXISTS created_by;
ALTER TABLE loan_applications DROP COLUMN IF EXISTS created_by;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Loud refusal first (0017/0020/0035 precedent), documented in the
    # module docstring; a clean expansion reverses without loss.
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN)
