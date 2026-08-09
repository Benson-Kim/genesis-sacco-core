"""claim-cap trigger locking probe: parent FOR UPDATE

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-02

Successor revision to 0030 backing the maintainer-
follow-up (carried onto as its F1, dispositioned to this micro-MR).
Strictly ADDITIVE defence-in-depth — no gate, oracle or guard is
weakened, and NO money moves.

THE WINDOW BEING CLOSED: 0030's ``check_recovery_within_claim``
constraint trigger validated the claim cap under plain MVCC reads.
The SERVICE path (``corrections.record_recovery_receipt``) is fully
serialised on the ``loan_write_offs`` row FOR UPDATE it takes first,
but two CONCURRENT DIRECT-SQL receipt inserts could each pass the cap
check against snapshots that exclude each other — the backstop only
stopped single-statement overshoot. This revision regenerates the
trigger function so the parent lookup is ``SELECT... FOR UPDATE``:

  * a DIRECT-SQL inserter now serialises on the claim anchor — the
    second insert blocks until the first commits, its subsequent SUM
    statement takes a fresh snapshot (READ COMMITTED plpgsql: one
    snapshot per statement) that INCLUDES the winner's row, and the
    cap check refuses loudly. The last theoretical over-recovery
    window is closed (falsifiable: tests/test_loan_recoveries.py
    concurrent-insert test — exactly one of two cap-busting inserts
    lands; fails with this FOR UPDATE removed).
  * the SERVICE path pays NOTHING: it already holds that exact row
    lock from its own anchor-first chain, and re-acquisition inside
    the same transaction is free (a self-owned lock creates no wait edge — the lock-order.md section
    4 self-owned-lock argument; the section 8 no-locking-probe note recorded for 0030 is
    deliberately reversed for the WOFF anchor in the same MR, the diagram-drift rule).

The explicit tenant predicate on the parent lookup stays (the 0014
rationale: tenant-safe even under a privileged role), and every other
statement of the function is byte-identical to 0030's.

Downgrade needs NO data guard (stated honestly, the 0017/0020
discipline applied): neither direction creates, alters or drops a
single row — both are ``CREATE OR REPLACE FUNCTION`` of the trigger
body only. The downgrade restores the exact 0030 function text (the
weaker MVCC-only backstop); the receipt history itself remains fully
protected by the untouched 0030 append-only triggers, so there is no
money history a refusal guard could protect. CI's migrate-check
(up -> down -> up) exercises both directions.
"""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

# The regenerated function: identical to 0030's except the parent
# lookup locks the claim anchor (FOR UPDATE OF w). See the module
# docstring for the concurrency argument.
_UP = """
CREATE OR REPLACE FUNCTION check_recovery_within_claim() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    claim numeric;
    wo_status text;
    recovered numeric;
BEGIN
    -- !51 N1: lock the claim anchor. The service already holds this
    -- row FOR UPDATE (re-acquisition is free); a direct-SQL inserter
    -- serialises here, so the SUM below always runs on a snapshot
    -- that includes every committed competitor.
    SELECT w.total_written_off, w.status INTO claim, wo_status
    FROM loan_write_offs w
    WHERE w.id = NEW.write_off_id AND w.tenant_id = NEW.tenant_id
    FOR UPDATE OF w;
    IF claim IS NULL THEN
        RAISE EXCEPTION
            'recovery receipt % references no same-tenant write-off (issue #21)',
            NEW.id;
    END IF;
    IF wo_status <> 'posted' THEN
        RAISE EXCEPTION
            'recovery receipts apply to POSTED write-offs only (issue #21): '
            'write-off % is ''%''', NEW.write_off_id, wo_status;
    END IF;
    SELECT COALESCE(SUM(r.amount), 0) INTO recovered
    FROM loan_recoveries r
    WHERE r.write_off_id = NEW.write_off_id AND r.tenant_id = NEW.tenant_id;
    IF recovered > claim THEN
        RAISE EXCEPTION
            'recovery receipts for write-off % would exceed the written-off '
            'claim (issue #21)', NEW.write_off_id;
    END IF;
    RETURN NULL;
END
$fn$;
"""

# The exact 0030 function text (the weaker MVCC-only backstop) — see
# the docstring for why no data guard applies.
_DOWN = """
CREATE OR REPLACE FUNCTION check_recovery_within_claim() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    claim numeric;
    wo_status text;
    recovered numeric;
BEGIN
    SELECT w.total_written_off, w.status INTO claim, wo_status
    FROM loan_write_offs w
    WHERE w.id = NEW.write_off_id AND w.tenant_id = NEW.tenant_id;
    IF claim IS NULL THEN
        RAISE EXCEPTION
            'recovery receipt % references no same-tenant write-off (issue #21)',
            NEW.id;
    END IF;
    IF wo_status <> 'posted' THEN
        RAISE EXCEPTION
            'recovery receipts apply to POSTED write-offs only (issue #21): '
            'write-off % is ''%''', NEW.write_off_id, wo_status;
    END IF;
    SELECT COALESCE(SUM(r.amount), 0) INTO recovered
    FROM loan_recoveries r
    WHERE r.write_off_id = NEW.write_off_id AND r.tenant_id = NEW.tenant_id;
    IF recovered > claim THEN
        RAISE EXCEPTION
            'recovery receipts for write-off % would exceed the written-off '
            'claim (issue #21)', NEW.write_off_id;
    END IF;
    RETURN NULL;
END
$fn$;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
