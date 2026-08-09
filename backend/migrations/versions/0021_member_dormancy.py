"""member dormancy lifecycle

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-01

Expand-only revision backing the build plan (claimed as 0021 with down_revision 0020 per the
migration-declaration rule — 0020 is dividends head).

  * members.status CHECK expanded (expand-only) with 'dormant'. The
    member status machine gains Active -> Dormant (nightly job only,
    when no member-initiated transaction fell inside the tenant's
    configured dormancy window), Dormant -> Active (automatic, inside
    the deposit transaction) and Dormant -> Exited (through the real settlement workflow). All
    writers go through the single
    domain transition function (concurrency safety).

  * idx_members_dormancy_scan — the dormancy batch scan's driving
    index (scalability: the index ships in the SAME migration as the query it serves,
    application/dormancy.py:dormancy_scan_sql).
    Partial over status = 'active' because only active members are
    dormancy candidates: dormant/exited members never widen the scan,
    which is also what makes the job's re-run a lock-free no-op
    (the status predicate itself is the anti-join's
    first leg). The member-initiated-activity NOT EXISTS anti-join is
    served by the existing idx_txns_member_keyset (0008).

No new tables: no TENANT_TABLES delta, no new RLS policies (members
kept its 0001 tenant_isolation policy), and the leakage suite already
covers members. Audit rows use entity='members', already mapped in
ENTITY_MODULES.

Downgrade drops the index and restores the narrower CHECK. Restoring
the narrower CHECK VALIDATES existing rows, so a database that already
holds dormant members refuses the downgrade loudly (the 0017/0020
conditional-refusal precedent — member state is never silently
rewritten). CI's migrate-check (up -> down -> up) runs on a database
without dormant members and passes.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_UP = """
-- Member status machine: 'dormant' (expand-only, the 0020 CHECK shape).
ALTER TABLE members DROP CONSTRAINT members_status_check;
ALTER TABLE members ADD CONSTRAINT members_status_check
    CHECK (status IN ('active', 'arrears', 'dormant', 'exited'));

-- The dormancy batch scan's driving index (gate 1.3: shipped with its
-- query, application/dormancy.py). Keyset walk in id order over the
-- candidate population only.
CREATE INDEX idx_members_dormancy_scan
    ON members (tenant_id, id)
    WHERE status = 'active';
"""

_DOWN = """
DROP INDEX IF EXISTS idx_members_dormancy_scan;

ALTER TABLE members DROP CONSTRAINT members_status_check;
-- Restoring the narrower CHECK validates existing rows: a database
-- holding dormant members refuses this loudly (the 0017/0020
-- conditional-refusal precedent) instead of silently rewriting member
-- state. Reactivate (deposit) or exit (P12) dormant members first.
ALTER TABLE members ADD CONSTRAINT members_status_check
    CHECK (status IN ('active', 'arrears', 'exited'));
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
