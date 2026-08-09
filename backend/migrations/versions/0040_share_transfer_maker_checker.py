"""share-transfer maker-checker workflow + history register

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-07

Expand-only revision backing the HUMAN-AUTHORIZED
remediation (the human-review findings (l) HIGH and (m) MEDIUM, recorded on ledger and item G2),
claimed as 0040 with
down_revision '0038' in its MR description at branch time (the migration-declaration rule; head
0038 + the single in-flight 0039 claim verified — declared plan: when merges, this revision
RE-CHAINS its down_revision to '0039' on the branch, the /0017 re-chain precedent; never renumber
another track's claim). RE-CHAINED to
'0039' as declared: the merge landed 0039_member_dividend_payout
on main and this branch plain-merged it.

  * share_transfers becomes the TWO-PHASE maker-checker workflow row
    (the issue-/0031 repayment-adjustments pattern — the strongest house SoD precedent,
    DB-backstopped): the MAKER's request INSERTs
    a PENDING row bound to a persisted snapshot; a DISTINCT CHECKER
    approves — re-verifying every snapshot component under the full
    lock set — before any money moves.

      - status: 'pending' | 'posted' | 'rejected'. Pre-0040 rows were
        executed by the single-actor flow the moment they were created
        — backfilled 'posted' with a NULL approver (truthful history,
        the 0031/0036 rule: an approver is never invented).
      - approved_by (FK users, ON DELETE RESTRICT) + decided_at — the
        checker attribution of the decision.
      - from_balance_at_request — the persisted approval snapshot the
        checker binds to; approval re-verifies it under
        the transferor's share-account lock and 409s on drift.
      - version + updated_at — optimistic locking for the rejection
        path (the house 409-on-stale rule).
      - out_transaction_id / in_transaction_id become NULLABLE: a
        pending or rejected transfer has posted NOTHING. The
        ck_share_transfers_txns_iff_posted CHECK ties them to the
        status machine: posted rows carry BOTH ledger transactions,
        non-posted rows carry NONE.
      - ck_share_transfers_sod — DB-level segregation of duties (the
        0031 ck_repayment_adjustments_sod shape, collusion-resistant):
        approved_by IS NULL OR approved_by <> created_by. The
        server-side guard is primary; this CHECK makes a
        maker-approved-own row unrepresentable even via direct SQL.
      - ck_share_transfers_pending_snapshot — a pending request ALWAYS
        carries the snapshot the checker will approve; only the
        backfilled posted rows are exempt (the 0031 shape).
      - write-once + status-machine trigger (the 0025/0031 discipline):
        identity/money/snapshot columns pinned after INSERT, one-shot
        NULL -> value fills for the decision columns, and the ONLY
        legal status move is pending -> posted | rejected — a terminal
        row can never move again even via direct SQL.

  * idx_share_transfers_register — (tenant_id, (status = 'pending')
    DESC, created_at DESC, id DESC): the ledger-(m) history register's
    serving index, shipped in the SAME MR as the keyset query it
    serves (scalability — the 0038 band-expression pattern: the checker's job order is PENDING
    FIRST, newest first inside each band; EXPLAIN-asserted, falsifiable by dropping it). Index audit
    first
    (v1.1 discipline): 0020 ships (tenant_id, from_member_id,...) and
    (tenant_id, to_member_id,...) — NEITHER serves a tenant-wide
    register-ordered keyset walk. The boolean band expression is an
    IMMUTABLE text comparison over the status CHECK vocabulary pinned
    here — a vocabulary change requires a successor migration that
    regenerates the expression in the same MR (the 0038 note).

  * idx_share_transfers_approved_by — (tenant_id, approved_by): the FK
    index rule (scalability), mirroring 0031's checker index.

  * NO RLS changes: share_transfers already carries forced RLS (0020);
    TENANT_TABLES and the leakage suite are unchanged.

Downgrade REFUSES LOUDLY (the 0017/0020/0031/0035/0036 precedent) when
maker-checker workflow history exists: any row still in the workflow
(pending/rejected) or any recorded checker attribution
(approved_by IS NOT NULL) is audit-relevant history — the N1-class
four-eyes control itself — and is never silently destroyed. On a clean
expansion (migrate-check's up -> down -> up) it reverses exactly,
restoring the 0020 single-phase shape.
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. Workflow + snapshot columns (expand-only; the 0031 shape)
-- ---------------------------------------------------------------------------
ALTER TABLE share_transfers
    ADD COLUMN status text,
    ADD COLUMN approved_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    ADD COLUMN decided_at timestamptz,
    ADD COLUMN from_balance_at_request numeric(18,2)
        CHECK (from_balance_at_request >= 0),
    ADD COLUMN version integer NOT NULL DEFAULT 1,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

-- Backfill: pre-0040 rows were executed by the single-actor flow the
-- moment they were created — they are 'posted', with NO approver and
-- NO request-time snapshot (truthful history, never invented).
UPDATE share_transfers SET status = 'posted' WHERE status IS NULL;

-- A pending or rejected transfer has posted nothing: the ledger
-- transaction columns become nullable, tied to the status machine by
-- CHECK below.
ALTER TABLE share_transfers
    ALTER COLUMN out_transaction_id DROP NOT NULL,
    ALTER COLUMN in_transaction_id DROP NOT NULL;

ALTER TABLE share_transfers
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'pending',
    ADD CONSTRAINT ck_share_transfers_status
        CHECK (status IN ('pending', 'posted', 'rejected')),
    -- DB-level segregation of duties (issue #31 (l) / !47 B2): a maker
    -- can never be their own checker, even via direct SQL. created_by
    -- is nullable (pre-attribution history): a NULL maker row passes
    -- (NULL <> x is NULL, and only FALSE fails a CHECK) — exactly the
    -- 0031 posture for NULL columns.
    ADD CONSTRAINT ck_share_transfers_sod
        CHECK (approved_by IS NULL OR approved_by <> created_by),
    -- Posted rows carry BOTH ledger transactions; non-posted carry NONE.
    ADD CONSTRAINT ck_share_transfers_txns_iff_posted
        CHECK ((status = 'posted')
               = (out_transaction_id IS NOT NULL
                  AND in_transaction_id IS NOT NULL)),
    -- A pending request ALWAYS carries the snapshot the checker will
    -- approve; only the backfilled posted rows are exempt.
    ADD CONSTRAINT ck_share_transfers_pending_snapshot
        CHECK (status <> 'pending' OR from_balance_at_request IS NOT NULL);

CREATE INDEX idx_share_transfers_approved_by
    ON share_transfers (tenant_id, approved_by);

-- ---------------------------------------------------------------------------
-- 2. The ledger-(m) register's serving index (the 0038 band pattern):
--    PENDING FIRST (the checker's job order), newest first inside each
--    band — shipped with the keyset query it serves (gate 1.3).
-- ---------------------------------------------------------------------------
CREATE INDEX idx_share_transfers_register
    ON share_transfers
        (tenant_id, (status = 'pending') DESC, created_at DESC, id DESC);

-- ---------------------------------------------------------------------------
-- 3. Write-once + status-machine trigger (the 0025/0031 discipline)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_share_transfer_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    -- Pinned after INSERT: identity, money, and the approval snapshot.
    IF (NEW.tenant_id, NEW.from_member_id, NEW.to_member_id, NEW.amount,
        NEW.created_by, NEW.created_at, NEW.from_balance_at_request)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.from_member_id, OLD.to_member_id, OLD.amount,
        OLD.created_by, OLD.created_at, OLD.from_balance_at_request)
    THEN
        RAISE EXCEPTION
            'share transfer % is write-once (issue #31 (l))', OLD.id;
    END IF;
    -- One-shot NULL -> value: the workflow fills each exactly once.
    IF (OLD.out_transaction_id IS NOT NULL
        AND NEW.out_transaction_id IS DISTINCT FROM OLD.out_transaction_id)
       OR (OLD.in_transaction_id IS NOT NULL
           AND NEW.in_transaction_id IS DISTINCT FROM OLD.in_transaction_id)
       OR (OLD.approved_by IS NOT NULL
           AND NEW.approved_by IS DISTINCT FROM OLD.approved_by)
       OR (OLD.decided_at IS NOT NULL
           AND NEW.decided_at IS DISTINCT FROM OLD.decided_at)
    THEN
        RAISE EXCEPTION
            'share transfer % decision fields are write-once (issue #31 (l))',
            OLD.id;
    END IF;
    -- The status machine at the database: the ONLY legal move is
    -- pending -> posted | rejected; terminal states are terminal
    -- (un-posting a posted transfer via SQL raises here).
    IF NEW.status IS DISTINCT FROM OLD.status
       AND (OLD.status IS DISTINCT FROM 'pending'
            OR NEW.status NOT IN ('posted', 'rejected'))
    THEN
        RAISE EXCEPTION
            'share transfer % status cannot move ''%'' -> ''%'' (issue #31 (l))',
            OLD.id, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER share_transfers_write_once
    BEFORE UPDATE ON share_transfers
    FOR EACH ROW EXECUTE FUNCTION forbid_share_transfer_mutation();
"""

#: The loud-refusal guard, exposed separately so the falsifiability
#: test can execute it against a database holding workflow history
#: (the 0017/0031 _DOWN_GUARD precedent).
_DOWN_GUARD = """
DO $guard$
DECLARE
    workflow_rows bigint;
    approved_rows bigint;
BEGIN
    SELECT count(*) INTO workflow_rows
        FROM share_transfers WHERE status <> 'posted';
    SELECT count(*) INTO approved_rows
        FROM share_transfers WHERE approved_by IS NOT NULL;
    IF workflow_rows > 0 OR approved_rows > 0 THEN
        RAISE EXCEPTION USING MESSAGE = format(
            'refusing downgrade 0040: share-transfer maker-checker '
            'workflow history exists (%s rows still in the workflow, '
            '%s rows with recorded checker attribution). Dropping the '
            'four-eyes record would recreate the !77 insider-fraud '
            'finding (issue #31 (l)) as data loss (the 0017/0031 '
            'refusal precedent) - workflow history is never silently '
            'destroyed.',
            workflow_rows, approved_rows);
    END IF;
END
$guard$;
"""

_DOWN = """
DROP TRIGGER share_transfers_write_once ON share_transfers;
DROP FUNCTION forbid_share_transfer_mutation();
DROP INDEX idx_share_transfers_register;
DROP INDEX idx_share_transfers_approved_by;
ALTER TABLE share_transfers
    DROP CONSTRAINT ck_share_transfers_pending_snapshot,
    DROP CONSTRAINT ck_share_transfers_txns_iff_posted,
    DROP CONSTRAINT ck_share_transfers_sod,
    DROP CONSTRAINT ck_share_transfers_status;
-- The guard above proved only 'posted' rows remain, and the
-- txns-iff-posted CHECK proved they all carry both transactions, so
-- restoring the 0020 NOT NULLs validates cleanly.
ALTER TABLE share_transfers
    ALTER COLUMN out_transaction_id SET NOT NULL,
    ALTER COLUMN in_transaction_id SET NOT NULL;
ALTER TABLE share_transfers
    DROP COLUMN updated_at,
    DROP COLUMN version,
    DROP COLUMN from_balance_at_request,
    DROP COLUMN decided_at,
    DROP COLUMN approved_by,
    DROP COLUMN status;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Loud refusal first (0017/0031 precedent), documented in the
    # module docstring; a clean expansion reverses without loss.
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN)
