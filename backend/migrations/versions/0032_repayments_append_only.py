"""append-only trigger discipline for repayments rows

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-02

Expand-only revision backing (the maintainer- follow-up). `repayments` rows had no append-only
trigger — unlike
ledger_entries (0004/0014) and the 0025/0030 correction tables. With
negative correction rows representable since 0025 (amount <> 0), a
manual SQL UPDATE flipping a row's sign was a FORENSIC HOLE: the
servicing history could be edited without any trigger firing while the
ledger legs stayed unchanged — exactly the divergence class the
 reconstruction self-check catches, but only at adjustment time.

This revision extends the 0004/0014 trigger discipline to repayments:
BEFORE UPDATE and BEFORE DELETE raise via the 0001
``forbid_row_mutation()`` function (reused — the 0004/0030 precedent).
Corrections REMAIN negative-linked NEW rows, never edits (data integrity):
grep-verified at authoring, no code path anywhere issues an UPDATE or
DELETE against repayments — the only writers are the INSERTs in
application/ledger.py (post_repayment / post_allocated_repayment) and
the negative correction-row INSERT in application/corrections.py.

Downgrade REFUSES LOUDLY when any negative (storno) repayments row
exists: dropping the triggers on a database that holds correction
history would re-open the forensic hole this revision closes for
exactly the rows that document past corrections (the 0017/0020/0025
conditional-refusal precedent). CI's migrate-check (up -> down -> up)
runs on a database without such rows and passes.
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_UP = """
-- APPEND-ONLY (the 0004/0014 ledger discipline, extended to the
-- servicing history per issue #24 N4): repayments rows are money
-- history; corrections are negative-linked NEW rows, never edits.
-- forbid_row_mutation() is defined in migration 0001 and reused here.
CREATE TRIGGER repayments_no_update
    BEFORE UPDATE ON repayments
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();
CREATE TRIGGER repayments_no_delete
    BEFORE DELETE ON repayments
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();
"""

#: The loud-refusal guard, exposed separately so the falsifiability
#: test can execute it against a database holding correction history
#: (the 0017 _DOWN_GUARD precedent).
_DOWN_GUARD = """
-- LOUD REFUSAL (0017/0020/0025 precedent): a negative row IS recorded
-- correction history — the forensic evidence the triggers protect.
-- Dropping the append-only discipline over it is refused.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM repayments WHERE amount < 0) THEN
        RAISE EXCEPTION
            'refusing downgrade: repayments holds negative (storno) '
            'correction rows (migration 0032) — the append-only triggers '
            'protecting that history are never silently dropped';
    END IF;
END
$$;
"""

_DOWN = (
    _DOWN_GUARD
    + """
DROP TRIGGER repayments_no_delete ON repayments;
DROP TRIGGER repayments_no_update ON repayments;
"""
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
