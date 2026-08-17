"""schema v1: core tables, constraints, indexes, forced RLS tenancy

Revision ID: 0001
Revises:
Create Date: 2026-07-27
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_UP = """
-- gen_random_uuid() is CORE PostgreSQL from 13 onward, which is below
-- this project's floor, so pgcrypto is a legacy fallback and nothing in
-- this schema calls a pgcrypto-only function (crypt/gen_salt/digest/
-- pgp_*). Guarded on availability because managed hosts routinely ship
-- PostgreSQL without the contrib package: an unguarded CREATE EXTENSION
-- aborts the very first migration with
--   could not open extension control file ".../pgcrypto.control"
-- (observed on a real cPanel host running 13.23, where pgcrypto is not
-- even listed in pg_available_extensions). Hosts that DO have it still
-- install it, so behaviour there is unchanged.
DO $pgcrypto$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgcrypto') THEN
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
    END IF;
END
$pgcrypto$;

CREATE TABLE tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    slug text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    name text NOT NULL,
    is_system boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE permissions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    role_id uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    module text NOT NULL,
    can_view boolean NOT NULL DEFAULT false,
    can_create boolean NOT NULL DEFAULT false,
    can_edit boolean NOT NULL DEFAULT false,
    can_approve boolean NOT NULL DEFAULT false,
    UNIQUE (tenant_id, role_id, module)
);
CREATE INDEX idx_permissions_role ON permissions (tenant_id, role_id);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    role_id uuid NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    full_name text NOT NULL,
    email text NOT NULL,
    phone text,
    branch text,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX idx_users_role ON users (tenant_id, role_id);

CREATE TABLE members (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_no text NOT NULL,
    type text NOT NULL
        CHECK (type IN ('person', 'company', 'group', 'vehicle')),
    name text NOT NULL,
    phone text,
    email text,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'arrears', 'exited')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, member_no)
);
CREATE INDEX idx_members_status ON members (tenant_id, status);
CREATE INDEX idx_members_type ON members (tenant_id, type);

CREATE TABLE share_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    balance numeric(18,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, member_id)
);

CREATE TABLE deposit_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    balance numeric(18,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, member_id)
);

CREATE TABLE loan_products (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    name text NOT NULL,
    rate_pct numeric(5,2) NOT NULL CHECK (rate_pct > 0 AND rate_pct <= 100),
    deposit_multiplier numeric(5,2) NOT NULL CHECK (deposit_multiplier > 0),
    max_term_months integer NOT NULL
        CHECK (max_term_months BETWEEN 1 AND 120),
    active boolean NOT NULL DEFAULT true,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE loan_applications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    product_id uuid NOT NULL REFERENCES loan_products(id) ON DELETE RESTRICT,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    term_months integer NOT NULL CHECK (term_months > 0),
    rate_pct numeric(5,2) NOT NULL CHECK (rate_pct > 0 AND rate_pct <= 100),
    purpose text,
    stage text NOT NULL DEFAULT 'submitted'
        CHECK (stage IN
            ('submitted', 'appraisal', 'committee',
             'approved', 'rejected', 'disbursed')),
    cover_pct numeric(6,2) NOT NULL DEFAULT 0 CHECK (cover_pct >= 0),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_applications_stage ON loan_applications (tenant_id, stage);
CREATE INDEX idx_applications_member ON loan_applications (tenant_id, member_id);
CREATE INDEX idx_applications_product ON loan_applications (tenant_id, product_id);

CREATE TABLE loans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    application_id uuid NOT NULL UNIQUE
        REFERENCES loan_applications(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    product_id uuid NOT NULL REFERENCES loan_products(id) ON DELETE RESTRICT,
    principal numeric(18,2) NOT NULL CHECK (principal > 0),
    balance numeric(18,2) NOT NULL CHECK (balance >= 0),
    rate_pct numeric(5,2) NOT NULL CHECK (rate_pct > 0 AND rate_pct <= 100),
    term_months integer NOT NULL CHECK (term_months > 0),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'closed', 'written_off')),
    classification text NOT NULL DEFAULT 'normal'
        CHECK (classification IN
            ('normal', 'watch', 'substandard', 'doubtful', 'loss')),
    days_past_due integer NOT NULL DEFAULT 0 CHECK (days_past_due >= 0),
    provision_pct numeric(5,2) NOT NULL DEFAULT 1
        CHECK (provision_pct BETWEEN 0 AND 100),
    disbursed_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_loans_status ON loans (tenant_id, status);
CREATE INDEX idx_loans_classification ON loans (tenant_id, classification);
CREATE INDEX idx_loans_member ON loans (tenant_id, member_id);
CREATE INDEX idx_loans_product ON loans (tenant_id, product_id);

CREATE TABLE loan_schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
    installment_no integer NOT NULL CHECK (installment_no > 0),
    due_date date NOT NULL,
    principal_due numeric(18,2) NOT NULL CHECK (principal_due >= 0),
    interest_due numeric(18,2) NOT NULL CHECK (interest_due >= 0),
    total_due numeric(18,2) NOT NULL CHECK (total_due >= 0),
    paid_amount numeric(18,2) NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    UNIQUE (tenant_id, loan_id, installment_no)
);
CREATE INDEX idx_schedules_due ON loan_schedules (tenant_id, due_date);
CREATE INDEX idx_schedules_loan ON loan_schedules (tenant_id, loan_id);

CREATE TABLE transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    txn_ref text NOT NULL,
    member_id uuid REFERENCES members(id) ON DELETE RESTRICT,
    type text NOT NULL
        CHECK (type IN
            ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
             'loan_repayment', 'interest_posting', 'exit_settlement')),
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    channel text NOT NULL
        CHECK (channel IN ('mpesa', 'bank', 'accrual', 'internal')),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, txn_ref)
);
CREATE INDEX idx_transactions_member
    ON transactions (tenant_id, member_id, occurred_at);
CREATE INDEX idx_transactions_time ON transactions (tenant_id, occurred_at);

CREATE TABLE ledger_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    transaction_id uuid NOT NULL
        REFERENCES transactions(id) ON DELETE RESTRICT,
    account text NOT NULL,
    side text NOT NULL CHECK (side IN ('debit', 'credit')),
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ledger_txn ON ledger_entries (tenant_id, transaction_id);
CREATE INDEX idx_ledger_account
    ON ledger_entries (tenant_id, account, created_at);

CREATE TABLE repayments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    transaction_id uuid NOT NULL
        REFERENCES transactions(id) ON DELETE RESTRICT,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_repayments_loan ON repayments (tenant_id, loan_id);

CREATE TABLE guarantees (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    guarantor_member_id uuid NOT NULL
        REFERENCES members(id) ON DELETE RESTRICT,
    borrower_member_id uuid NOT NULL
        REFERENCES members(id) ON DELETE RESTRICT,
    application_id uuid REFERENCES loan_applications(id) ON DELETE RESTRICT,
    loan_id uuid REFERENCES loans(id) ON DELETE RESTRICT,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    status text NOT NULL DEFAULT 'pledged'
        CHECK (status IN ('pledged', 'active', 'released')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (guarantor_member_id <> borrower_member_id)
);
CREATE INDEX idx_guarantees_guarantor
    ON guarantees (tenant_id, guarantor_member_id);
CREATE INDEX idx_guarantees_borrower
    ON guarantees (tenant_id, borrower_member_id);
CREATE INDEX idx_guarantees_loan ON guarantees (tenant_id, loan_id);
CREATE INDEX idx_guarantees_application ON guarantees (tenant_id, application_id);

CREATE TABLE member_exits (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'approved', 'settled', 'rejected')),
    shares_amount numeric(18,2) NOT NULL DEFAULT 0 CHECK (shares_amount >= 0),
    deposits_amount numeric(18,2) NOT NULL DEFAULT 0
        CHECK (deposits_amount >= 0),
    loan_balance numeric(18,2) NOT NULL DEFAULT 0 CHECK (loan_balance >= 0),
    fees numeric(18,2) NOT NULL DEFAULT 0 CHECK (fees >= 0),
    net_payable numeric(18,2) NOT NULL DEFAULT 0,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_exits_member ON member_exits (tenant_id, member_id);
CREATE INDEX idx_exits_status ON member_exits (tenant_id, status);

CREATE TABLE outbox_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'dispatched', 'dead')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz
);
CREATE INDEX idx_outbox_pending ON outbox_events (next_attempt_at)
    WHERE status = 'pending';

CREATE TABLE audit_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    actor_id uuid,
    action text NOT NULL,
    entity text NOT NULL,
    entity_id text NOT NULL,
    before jsonb,
    after jsonb,
    ip text,
    at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_log (tenant_id, entity, entity_id);
CREATE INDEX idx_audit_time ON audit_log (tenant_id, at);

CREATE TABLE idempotency_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    key text NOT NULL,
    request_hash text NOT NULL,
    response_status integer,
    response_body jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);

CREATE TABLE otp_challenges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    attempts integer NOT NULL DEFAULT 0
        CHECK (attempts >= 0 AND attempts <= 5),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_otp_user ON otp_challenges (tenant_id, user_id, created_at);

CREATE FUNCTION forbid_row_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'table % is append-only (MASTER_PROMPT gate 1.5)',
        TG_TABLE_NAME;
END
$fn$;

CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_row_mutation();

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants
    USING (id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (id = current_setting('app.tenant_id')::uuid);

DO $rls$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'roles', 'permissions', 'users', 'members', 'share_accounts',
        'deposit_accounts', 'loan_products', 'loan_applications', 'loans',
        'loan_schedules', 'transactions', 'ledger_entries', 'repayments',
        'guarantees', 'member_exits', 'outbox_events', 'audit_log',
        'idempotency_keys', 'otp_challenges'
    ]
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
DROP TABLE IF EXISTS
    otp_challenges, idempotency_keys, audit_log, outbox_events,
    member_exits, guarantees, repayments, ledger_entries, transactions,
    loan_schedules, loans, loan_applications, loan_products,
    deposit_accounts, share_accounts, members, users, permissions,
    roles, tenants
CASCADE;
DROP FUNCTION IF EXISTS forbid_row_mutation();
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
