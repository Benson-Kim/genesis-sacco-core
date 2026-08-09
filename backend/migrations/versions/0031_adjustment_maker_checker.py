"""maker-checker approval workflow for repayment adjustments

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-02

Expand-only revision backing (the maintainer- follow-up): repayment adjustments below the authority
band were
single-actor money mutations; this revision makes the core-banking
maker-checker standard REPRESENTABLE AND ENFORCED at the database —
the maker creates a PENDING adjustment bound to a persisted snapshot,
and a DISTINCT checker approves before the reversal posts (the P9/ write-off shape: persisted
snapshot + separate approver + snapshot-bind-reverify).

  * repayment_adjustments.status — the adjustment workflow machine:
    'pending_approval' -> 'posted' | 'rejected'. EXISTING rows are
    backfilled 'posted' with a NULL checker_id: they were executed by
    the pre-N1 single-actor flow and the history stays TRUTHFUL — a
    backfill inventing a checker would be forgery, and rewriting the
    rows is impossible anyway (they are money history). New rows
    default 'pending_approval'.

  * checker_id (FK users, ON DELETE RESTRICT) + decided_at — each
    movable exactly ONCE from NULL (the write-once trigger below), so
    a decided adjustment can never be silently re-attributed.

  * DB-LEVEL SEGREGATION OF DUTIES (the B2 principle as a collusion-resistant CHECK):
  ck_repayment_adjustments_sod —
    checker_id IS NULL OR checker_id <> maker_id. The server-side
    guard in approve/reject refuses first; this CHECK makes a
    maker-approved-own row unrepresentable even through direct SQL on
    the app role. (The assurance-role/auditor exclusion is enforced
    server-side — role NAMES are not visible to a CHECK.)

  * PERSISTED APPROVAL SNAPSHOT: the loan figures the
    checker approves — loan_balance_at_request,
    loan_penalty_due_at_request (both >= 0) and
    loan_status_at_request ('active' | 'closed'; written_off can never
    be requested). ck_repayment_adjustments_snapshot forces the
    snapshot NOT NULL on pending rows; the backfilled 'posted' rows
    are exempt (their request-time figures were never captured —
    truthful history, never invented). Approval re-verifies every
    component under the full lock set and 409s on drift, posting
    NOTHING (the / snapshot-bind-reverify pattern).

  * version + updated_at — the reject path is an optimistic-locked
    mutation (409 on stale version, concurrency safety).

  * uq_repayment_adjustments_claim becomes a PARTIAL UNIQUE
    (tenant_id, repayment_id) WHERE status <> 'rejected': a REJECTED
    adjustment frees the slot for a fresh request (the
    uq_loan_write_offs_open shape), while pending/posted still block
    atomically (INSERT... ON CONFLICT DO NOTHING + rowcount, v1.1
    rule 5) — a repayment can never carry two live adjustments.

  * The write-once trigger is REGENERATED (the 0025 discipline),
    pinning every money/identity/snapshot column after INSERT
    (tenant/repayment/loan/original-transaction ids, maker, reason,
    amount/penalties/interest/principal, reopened_loan, created_at,
    and the three snapshot columns), permitting exactly one
    NULL -> value fill for reversal_transaction_id / checker_id /
    decided_at, and enforcing the status machine at the database:
    the ONLY legal move is pending_approval -> posted | rejected — a
    terminal status never moves again (un-posting via SQL raises).

Downgrade REFUSES LOUDLY when maker-checker workflow history exists —
any row with status <> 'posted' (pending/rejected requests are
audit-relevant workflow history) or with a non-NULL checker_id (the
checker attribution of a posted adjustment is the N1 control itself
and must never be silently dropped) — the 0017/0020 conditional-
refusal precedent. On a clean database it restores the exact 0025
trigger function, UNIQUE constraint and column set; CI's migrate-check
(up -> down -> up) runs on a database without such rows and passes.
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. Drop the 0025 write-once trigger for the duration of the DDL/backfill
--    (recreated below with the regenerated function; nothing else can
--    write concurrently — migrations run in their own transaction).
-- ---------------------------------------------------------------------------
DROP TRIGGER repayment_adjustments_write_once ON repayment_adjustments;

-- ---------------------------------------------------------------------------
-- 2. Workflow + snapshot columns (expand-only)
-- ---------------------------------------------------------------------------
ALTER TABLE repayment_adjustments
    ADD COLUMN status text,
    ADD COLUMN checker_id uuid REFERENCES users(id) ON DELETE RESTRICT,
    ADD COLUMN decided_at timestamptz,
    ADD COLUMN loan_balance_at_request numeric(18,2)
        CHECK (loan_balance_at_request >= 0),
    ADD COLUMN loan_penalty_due_at_request numeric(18,2)
        CHECK (loan_penalty_due_at_request >= 0),
    ADD COLUMN loan_status_at_request text
        CHECK (loan_status_at_request IN ('active', 'closed')),
    ADD COLUMN version integer NOT NULL DEFAULT 1,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

-- Backfill: pre-N1 rows were executed by the single-actor flow the
-- moment they were created — they are 'posted', with NO checker and
-- NO request-time snapshot (truthful history, never invented).
UPDATE repayment_adjustments SET status = 'posted' WHERE status IS NULL;

ALTER TABLE repayment_adjustments
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'pending_approval',
    ADD CONSTRAINT ck_repayment_adjustments_status
        CHECK (status IN ('pending_approval', 'posted', 'rejected')),
    -- DB-level segregation of duties (issue #24 / !47 B2): a maker
    -- can never be their own checker, even via direct SQL.
    ADD CONSTRAINT ck_repayment_adjustments_sod
        CHECK (checker_id IS NULL OR checker_id <> maker_id),
    -- A pending request ALWAYS carries the snapshot the checker will
    -- approve; only the backfilled posted rows are exempt.
    ADD CONSTRAINT ck_repayment_adjustments_snapshot
        CHECK (status <> 'pending_approval'
               OR (loan_balance_at_request IS NOT NULL
                   AND loan_penalty_due_at_request IS NOT NULL
                   AND loan_status_at_request IS NOT NULL));

CREATE INDEX idx_repayment_adjustments_checker
    ON repayment_adjustments (tenant_id, checker_id);

-- ---------------------------------------------------------------------------
-- 3. The one-adjustment-per-repayment claim becomes PARTIAL: rejected
--    rows free the slot; pending/posted still block atomically.
-- ---------------------------------------------------------------------------
ALTER TABLE repayment_adjustments
    DROP CONSTRAINT uq_repayment_adjustments_claim;
CREATE UNIQUE INDEX uq_repayment_adjustments_claim
    ON repayment_adjustments (tenant_id, repayment_id)
    WHERE status <> 'rejected';

-- ---------------------------------------------------------------------------
-- 4. Write-once trigger REGENERATED (the 0025 discipline): pinned
--    money/identity/snapshot columns, one-shot NULL -> value fills,
--    and the status machine enforced at the database.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_repayment_adjustment_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    -- Pinned after INSERT: identity, money, and the approval snapshot.
    IF (NEW.tenant_id, NEW.repayment_id, NEW.loan_id,
        NEW.original_transaction_id, NEW.maker_id, NEW.reason,
        NEW.amount, NEW.penalties, NEW.interest, NEW.principal,
        NEW.reopened_loan, NEW.created_at,
        NEW.loan_balance_at_request, NEW.loan_penalty_due_at_request,
        NEW.loan_status_at_request)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.repayment_id, OLD.loan_id,
        OLD.original_transaction_id, OLD.maker_id, OLD.reason,
        OLD.amount, OLD.penalties, OLD.interest, OLD.principal,
        OLD.reopened_loan, OLD.created_at,
        OLD.loan_balance_at_request, OLD.loan_penalty_due_at_request,
        OLD.loan_status_at_request)
    THEN
        RAISE EXCEPTION
            'repayment adjustment % is write-once (P13.15/issue #24)', OLD.id;
    END IF;
    -- One-shot NULL -> value: the workflow fills each exactly once.
    IF (OLD.reversal_transaction_id IS NOT NULL
        AND NEW.reversal_transaction_id
            IS DISTINCT FROM OLD.reversal_transaction_id)
       OR (OLD.checker_id IS NOT NULL
           AND NEW.checker_id IS DISTINCT FROM OLD.checker_id)
       OR (OLD.decided_at IS NOT NULL
           AND NEW.decided_at IS DISTINCT FROM OLD.decided_at)
    THEN
        RAISE EXCEPTION
            'repayment adjustment % decision fields are write-once (issue #24)',
            OLD.id;
    END IF;
    -- The status machine at the database: the ONLY legal move is
    -- pending_approval -> posted | rejected; terminal states are
    -- terminal (un-posting a posted adjustment via SQL raises here).
    IF NEW.status IS DISTINCT FROM OLD.status
       AND (OLD.status IS DISTINCT FROM 'pending_approval'
            OR NEW.status NOT IN ('posted', 'rejected'))
    THEN
        RAISE EXCEPTION
            'repayment adjustment % status cannot move ''%'' -> ''%'' (issue #24)',
            OLD.id, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER repayment_adjustments_write_once
    BEFORE UPDATE ON repayment_adjustments
    FOR EACH ROW EXECUTE FUNCTION forbid_repayment_adjustment_mutation();
"""

#: The loud-refusal guard, exposed separately so the falsifiability
#: test can execute it against a database holding workflow history
#: (the 0017 _DOWN_GUARD precedent).
_DOWN_GUARD = """
-- LOUD REFUSAL (0017/0020 precedent): pending/rejected requests are
-- audit-relevant workflow history, and the checker attribution of a
-- posted adjustment is the N1 control itself — neither is ever
-- silently dropped. Only a database whose every adjustment is a
-- checker-less 'posted' row (the pre-0031 shape) may downgrade.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM repayment_adjustments
               WHERE status <> 'posted' OR checker_id IS NOT NULL) THEN
        RAISE EXCEPTION
            'refusing downgrade: repayment_adjustments holds maker-checker '
            'workflow history (pending/rejected requests or checker '
            'attributions, migration 0031) — audit-relevant state is never '
            'silently rewritten';
    END IF;
END
$$;
"""

_DOWN = (
    _DOWN_GUARD
    + """
DROP TRIGGER repayment_adjustments_write_once ON repayment_adjustments;
DROP INDEX uq_repayment_adjustments_claim;
DROP INDEX idx_repayment_adjustments_checker;
ALTER TABLE repayment_adjustments
    DROP CONSTRAINT ck_repayment_adjustments_status,
    DROP CONSTRAINT ck_repayment_adjustments_sod,
    DROP CONSTRAINT ck_repayment_adjustments_snapshot,
    DROP COLUMN status,
    DROP COLUMN checker_id,
    DROP COLUMN decided_at,
    DROP COLUMN loan_balance_at_request,
    DROP COLUMN loan_penalty_due_at_request,
    DROP COLUMN loan_status_at_request,
    DROP COLUMN version,
    DROP COLUMN updated_at;
ALTER TABLE repayment_adjustments
    ADD CONSTRAINT uq_repayment_adjustments_claim UNIQUE (tenant_id, repayment_id);

-- Restore the EXACT 0025 trigger function (verbatim from that revision).
CREATE OR REPLACE FUNCTION forbid_repayment_adjustment_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF (NEW.tenant_id, NEW.repayment_id, NEW.loan_id,
        NEW.original_transaction_id, NEW.maker_id, NEW.reason,
        NEW.amount, NEW.penalties, NEW.interest, NEW.principal,
        NEW.reopened_loan, NEW.created_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.repayment_id, OLD.loan_id,
        OLD.original_transaction_id, OLD.maker_id, OLD.reason,
        OLD.amount, OLD.penalties, OLD.interest, OLD.principal,
        OLD.reopened_loan, OLD.created_at)
       OR (OLD.reversal_transaction_id IS NOT NULL
           AND NEW.reversal_transaction_id
               IS DISTINCT FROM OLD.reversal_transaction_id)
    THEN
        RAISE EXCEPTION
            'repayment adjustment % is write-once (P13.15)', OLD.id;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER repayment_adjustments_write_once
    BEFORE UPDATE ON repayment_adjustments
    FOR EACH ROW EXECUTE FUNCTION forbid_repayment_adjustment_mutation();
"""
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
