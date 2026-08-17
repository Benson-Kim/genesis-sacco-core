"""tenant settings, parameters & approval matrix (P13.7)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-31

Numbering note: this revision originally chained from 0015, reserving
0016 for the then-in-flight P13.6 branches registry. P13.6 has since
merged to main, so down_revision now points at 0016 and the alembic
chain is linear again (the re-chain recorded in the MR).

Expand-only revision backing BUILD_PROMPTS P13.7: generalise
tenant_settings (0009: deposit-interest rate; 0010: exit fee) into the
prototype Settings screens' backend (GAP_ANALYSIS 2.9). One row per
tenant (PK tenant_id — every settings read is a single PK lookup);
every column nullable, NULL = "not configured, consumers fall back to
the code-owned default". DB CHECKs on every bound (gate 1.5); the
management API (the single legitimate writer, gate 1.6 v1.1 rule 1)
revalidates the same bounds at the boundary.

  1. deposit_interest_annual_rate_pct DROP NOT NULL — the settings row
     now exists as soon as ANY setting is configured, so the deposit
     rate becomes optional like every other key. The 0009 CHECK stays
     (NULL satisfies a CHECK); the accrual job treats a NULL rate
     exactly like a missing row (409 unconfigured, unchanged contract).

  2. Interest config: dividend %, penalty rate %/mo, penalty grace
     days, penalty charged-on basis, loan interest method/basis
     (STORED ONLY — the P6 engine extension is explicitly out of
     scope, see BUILD_PROMPTS P13.7), tiered loan-rate bands (JSONB,
     validated at write AND revalidated at read in
     genesis/domain/tenant_config.py; the DB CHECK pins the top-level
     type so a manual edit cannot smuggle a scalar).

  3. Global parameters: min share capital, registration fee, min
     monthly contribution, max member exposure, dormancy period,
     financial year end month, exit notice period.

  4. Approval matrix: committee size, committee quorum (consumed live
     by P9 cast_vote and P12 cast_exit_vote), per-authority amount
     bands (JSONB, consumed live by the P9 stage machine).

  5. loan_products.guarantors_required — the prototype Settings > Loan
     products "guarantors required" field. NOT NULL DEFAULT 0 is
     backfill-safe for existing rows (expand-only). Stored config in
     this prompt; disbursement-time enforcement is recorded as a
     follow-up in the MR.

  6. roles: system role names become immutable (review R5). Approval-
     band authorities key off seeded role NAMES, so a rename would
     silently detach a configured money ceiling. No application path
     updates roles.name (application/rbac.py mutates permissions
     only); the trigger makes the invariant DB-enforced against manual
     SQL through the app role as well.

No new tables and no new indexes: tenant_settings keeps its 0009 PK
and RLS policy (forced, tenant_isolation), which serve every new read.

Downgrade (review R4 — non-destructive by contract): the new columns,
trigger and function are dropped and the NOT NULL on the deposit rate
is restored, but ONLY when no tenant_settings row holds a NULL deposit
rate. Such rows carry money parameters (penalty rates, approval
matrices, global parameters) configured under this revision, so the
downgrade REFUSES with an exception naming the manual step — set a
deposit rate per tenant via PUT /settings, or deliberately export and
delete the rows — instead of silently DELETEing configured money
config. CI's migrate-check (up -> down -> up) runs on a database
without such rows and is unaffected.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. The settings row no longer requires the deposit rate (P13.7)
-- ---------------------------------------------------------------------------
ALTER TABLE tenant_settings
    ALTER COLUMN deposit_interest_annual_rate_pct DROP NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Interest configuration (prototype Settings > Interest tab)
-- ---------------------------------------------------------------------------
ALTER TABLE tenant_settings
    ADD COLUMN dividend_rate_pct numeric(5,2)
        CHECK (dividend_rate_pct >= 0 AND dividend_rate_pct <= 100),
    ADD COLUMN penalty_rate_pct_per_month numeric(5,2)
        CHECK (penalty_rate_pct_per_month >= 0
               AND penalty_rate_pct_per_month <= 100),
    ADD COLUMN penalty_grace_days integer
        CHECK (penalty_grace_days >= 0 AND penalty_grace_days <= 365),
    ADD COLUMN penalty_charged_on text
        CHECK (penalty_charged_on IN ('instalment_in_arrears', 'full_outstanding')),
    ADD COLUMN loan_interest_method text
        CHECK (loan_interest_method IN ('reducing_balance', 'flat')),
    ADD COLUMN loan_interest_basis text
        CHECK (loan_interest_basis IN ('thirty_360', 'actual_365')),
    ADD COLUMN loan_rate_bands jsonb
        CHECK (jsonb_typeof(loan_rate_bands) = 'array');

-- ---------------------------------------------------------------------------
-- 3. Global parameters (prototype Settings > Parameters tab)
-- ---------------------------------------------------------------------------
ALTER TABLE tenant_settings
    ADD COLUMN min_share_capital numeric(18,2)
        CHECK (min_share_capital >= 0),
    ADD COLUMN registration_fee numeric(18,2)
        CHECK (registration_fee >= 0),
    ADD COLUMN min_monthly_contribution numeric(18,2)
        CHECK (min_monthly_contribution >= 0),
    ADD COLUMN max_member_exposure numeric(18,2)
        CHECK (max_member_exposure >= 0),
    ADD COLUMN dormancy_period_months integer
        CHECK (dormancy_period_months >= 1 AND dormancy_period_months <= 120),
    ADD COLUMN financial_year_end_month integer
        CHECK (financial_year_end_month >= 1 AND financial_year_end_month <= 12),
    ADD COLUMN exit_notice_period_days integer
        CHECK (exit_notice_period_days >= 0 AND exit_notice_period_days <= 365);

-- ---------------------------------------------------------------------------
-- 4. Approval matrix (prototype Settings > Approval matrix tab)
-- ---------------------------------------------------------------------------
ALTER TABLE tenant_settings
    ADD COLUMN committee_size integer
        CHECK (committee_size >= 1 AND committee_size <= 25),
    ADD COLUMN committee_quorum integer
        CHECK (committee_quorum >= 1 AND committee_quorum <= 25),
    ADD COLUMN approval_bands jsonb
        CHECK (jsonb_typeof(approval_bands) = 'array'),
    ADD CONSTRAINT ck_tenant_settings_quorum_le_size
        CHECK (committee_quorum IS NULL OR committee_size IS NULL
               OR committee_quorum <= committee_size);

-- ---------------------------------------------------------------------------
-- 5. Per-product guarantors-required (prototype Settings > Loan products)
-- ---------------------------------------------------------------------------
ALTER TABLE loan_products
    ADD COLUMN guarantors_required integer NOT NULL DEFAULT 0
        CHECK (guarantors_required >= 0 AND guarantors_required <= 10);

-- ---------------------------------------------------------------------------
-- 6. System role names are immutable (review R5): approval-band
--    authorities key off seeded role NAMES; a rename would silently
--    detach a configured money ceiling.
-- ---------------------------------------------------------------------------
CREATE FUNCTION refuse_system_role_rename() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.is_system AND NEW.name IS DISTINCT FROM OLD.name THEN
        RAISE EXCEPTION
            'system role names are immutable (P13.7 review R5): '
            'approval-band authorities key off them';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER roles_refuse_system_rename
    BEFORE UPDATE OF name ON roles
    FOR EACH ROW EXECUTE FUNCTION refuse_system_role_rename();
"""

#: Downgrade guard (review R4), exported separately so the test suite
#: can prove it refuses while rows with a NULL deposit rate exist —
#: falsifiable independently of a full downgrade run.
_DOWN_GUARD = """
DO $gd$
DECLARE null_rate_rows bigint;
BEGIN
    SELECT count(*) INTO null_rate_rows
        FROM tenant_settings
        WHERE deposit_interest_annual_rate_pct IS NULL;
    IF null_rate_rows > 0 THEN
        RAISE EXCEPTION
            'refusing 0017 downgrade: % tenant_settings row(s) hold a NULL '
            'deposit_interest_annual_rate_pct; downgrading would destroy their '
            'configured money parameters (penalty rates, approval matrices, '
            'global parameters). Manual step: set a deposit rate for each such '
            'tenant via PUT /settings, or deliberately export and delete the '
            'rows, then re-run the downgrade.',
            null_rate_rows;
    END IF;
END
$gd$;
"""

_DOWN = (
    _DOWN_GUARD
    + """
DROP TRIGGER IF EXISTS roles_refuse_system_rename ON roles;
DROP FUNCTION IF EXISTS refuse_system_role_rename();

ALTER TABLE loan_products DROP COLUMN IF EXISTS guarantors_required;

ALTER TABLE tenant_settings
    DROP CONSTRAINT IF EXISTS ck_tenant_settings_quorum_le_size;
ALTER TABLE tenant_settings
    DROP COLUMN IF EXISTS approval_bands,
    DROP COLUMN IF EXISTS committee_quorum,
    DROP COLUMN IF EXISTS committee_size,
    DROP COLUMN IF EXISTS exit_notice_period_days,
    DROP COLUMN IF EXISTS financial_year_end_month,
    DROP COLUMN IF EXISTS dormancy_period_months,
    DROP COLUMN IF EXISTS max_member_exposure,
    DROP COLUMN IF EXISTS min_monthly_contribution,
    DROP COLUMN IF EXISTS registration_fee,
    DROP COLUMN IF EXISTS min_share_capital,
    DROP COLUMN IF EXISTS loan_rate_bands,
    DROP COLUMN IF EXISTS loan_interest_basis,
    DROP COLUMN IF EXISTS loan_interest_method,
    DROP COLUMN IF EXISTS penalty_charged_on,
    DROP COLUMN IF EXISTS penalty_grace_days,
    DROP COLUMN IF EXISTS penalty_rate_pct_per_month,
    DROP COLUMN IF EXISTS dividend_rate_pct;

-- The guard above (review R4) has already refused the downgrade if any
-- row still holds a NULL deposit rate, so re-tightening cannot destroy
-- configured money parameters and cannot fail on existing data.
ALTER TABLE tenant_settings
    ALTER COLUMN deposit_interest_annual_rate_pct SET NOT NULL;
"""
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
