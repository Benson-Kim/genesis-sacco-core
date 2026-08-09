"""reports & exports: export jobs, artifacts, report-query indexes

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-29

Additive only (expand phase):

  * exports — one row per requested export job. The scope (report,
    filters, column allow-list, as-of instant) is resolved and frozen
    SERVER-SIDE at request time (blocker a/e): the worker renders
    exactly what the requesting user was entitled to see at that
    moment, never what a later caller asks for. status is the claim
    the job runner takes FOR UPDATE SKIP LOCKED inside its single
    rendering transaction, so an aborted job rolls back to
    'requested' with zero partial state (blocker i).

  * export_artifacts — the rendered CSV + PDF for one export.
    UNIQUE (export_id) makes "exactly one artifact per export" a
    DATABASE invariant, so the idempotency proof (blocker g) is
    enforced, not just tested. Download tokens are independent random
    values (never enumerable ids, blocker e) with their own UNIQUE
    indexes serving the download lookup; expires_at bounds the
    download window.

  * idx_exports_requested — the job runner's claim scan (partial: only unprocessed rows,
  scalability).
  * idx_txns_type_occurred — disbursement & collections report:
    keyset pages over (tenant, type, occurred_at, id) (scalability, shipped with the query that
    needs it).
  * idx_loans_disbursed — NPL-trend monthly reconstruction scans
    loans by disbursement date (partial: only disbursed loans).

RLS matches 0001 (forced, tenant_isolation policy).
Working downgrade drops only the new objects.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE exports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    report text NOT NULL CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement')),
    filters jsonb NOT NULL DEFAULT '{}'::jsonb,
    allowed_columns jsonb NOT NULL,
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'completed', 'failed')),
    requested_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    as_of timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_exports_requested
    ON exports (tenant_id, created_at, id)
    WHERE status = 'requested';
CREATE INDEX idx_exports_requested_by ON exports (tenant_id, requested_by);

CREATE TABLE export_artifacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    export_id uuid NOT NULL UNIQUE REFERENCES exports(id) ON DELETE CASCADE,
    csv_token text NOT NULL UNIQUE,
    pdf_token text NOT NULL UNIQUE,
    csv_content bytea NOT NULL,
    pdf_content bytea NOT NULL,
    row_count integer NOT NULL CHECK (row_count >= 0),
    truncated boolean NOT NULL,
    row_limit integer NOT NULL CHECK (row_limit > 0),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_txns_type_occurred
    ON transactions (tenant_id, type, occurred_at, id);

CREATE INDEX idx_loans_disbursed
    ON loans (tenant_id, disbursed_at)
    WHERE disbursed_at IS NOT NULL;

DO $rls$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['exports', 'export_artifacts']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I
            USING (tenant_id = current_setting(''app.tenant_id'')::uuid)
            WITH CHECK
            (tenant_id = current_setting(''app.tenant_id'')::uuid)', t);
    END LOOP;
END
$rls$;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_loans_disbursed;
DROP INDEX IF EXISTS idx_txns_type_occurred;
DROP TABLE IF EXISTS export_artifacts CASCADE;
DROP TABLE IF EXISTS exports CASCADE;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
