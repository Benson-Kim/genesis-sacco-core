"""accounting periods: table, RLS, closed-period trigger

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-29

Additive only (expand phase):

  * accounting_periods — the period model, per tenant. A row
    exists once a period has been CLOSED by the close action (absence of
    a row = the period is open); the status column keeps a future
    "reopen" action additive. Every ledger posting validates its
    occurred_at against this table server-side (application guard in
    ledger._post) — callers can never backdate into a finalised month
    (the caller-input lesson). UNIQUE (tenant_id, period_start)
    makes concurrent closes collapse to exactly one row (concurrency safety, claimed atomically by
    INSERT.. ON CONFLICT rowcount) and doubles
    as the closed-period lookup index for the hot posting path
    (scalability; the uq_member_exits_open precedent). RLS matches 0001.

  * transactions_closed_period trigger — the DB-level backstop
    (data integrity): even raw SQL that bypasses the application service
    cannot insert a transaction whose occurred_at falls inside a
    closed period. The application guard gives clean 409 semantics;
    this trigger makes the invariant hold at the database.

Working downgrade drops only the new objects.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE accounting_periods (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    period_start date NOT NULL,
    period_end date NOT NULL CHECK (period_end >= period_start),
    status text NOT NULL DEFAULT 'closed' CHECK (status IN ('open', 'closed')),
    closed_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    closed_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, period_start)
);

ALTER TABLE accounting_periods ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounting_periods FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON accounting_periods
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- DB-level backstop (gate 1.5): no transaction may land inside a
-- closed period, regardless of the code path that inserts it. The
-- application guard (ledger._post) raises the user-facing 409 first;
-- this trigger enforces the invariant for anything that slips past it.
CREATE FUNCTION forbid_closed_period_posting() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF EXISTS (
        SELECT 1 FROM accounting_periods p
        WHERE p.tenant_id = NEW.tenant_id
          AND p.status = 'closed'
          AND (NEW.occurred_at AT TIME ZONE 'UTC')::date >= p.period_start
          AND (NEW.occurred_at AT TIME ZONE 'UTC')::date <= p.period_end
    ) THEN
        RAISE EXCEPTION
            'transaction occurred_at falls inside a closed accounting period '
            '(MASTER_PROMPT gate 1.5; issue #12)';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER transactions_closed_period
    BEFORE INSERT ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_closed_period_posting();
"""

_DOWN = """
DROP TRIGGER IF EXISTS transactions_closed_period ON transactions;
DROP FUNCTION IF EXISTS forbid_closed_period_posting();
DROP TABLE IF EXISTS accounting_periods CASCADE;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
