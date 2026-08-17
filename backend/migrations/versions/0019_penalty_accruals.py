"""penalty accrual claim table (P13.8)

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-01

Expand-only revision backing BUILD_PROMPTS P13.8: the nightly arrears
job now accrues arrears penalties into ``loans.penalty_due`` (the 0007
receivable bucket consumed first by the P10 repayment allocation).
This table is the accrual's idempotency claim AND its reconstruction
record — no new loan columns, no schema change to existing tables.

  * penalty_accruals — exactly one row per (tenant, loan, accrual day),
    claimed with INSERT ... ON CONFLICT DO NOTHING checked by rowcount
    (v1.1 rule 5; the deposit_interest_accruals precedent from 0008).
    The accrual PERIOD is one UTC calendar day: each nightly run
    accrues exactly the as-of day's penalty, so the claim key
    (tenant_id, loan_id, accrual_date) makes every period
    write-once — a re-run or a concurrent second runner can never
    double-accrue the same day.

    The row records the exact figures the accrual was computed from
    (basis kind, basis amount, monthly rate, grace, days past due,
    amount), so every penny of ``loans.penalty_due`` is reconstructable
    even after the tenant's penalty configuration changes: a config
    change applies to FUTURE accrual days only, already-claimed days
    are never recomputed (v1.1 rule 3, the P13.7 quorum precedent).

    CHECKs (gate 1.5 — invariants in the DATABASE, not just the app):
      - amount > 0: zero-amount days are skipped, never claimed, so
        the table carries no noise rows and a re-run of a zero day
        stays a pure no-op;
      - days_past_due > grace_days: the DB backstop for the grace
        boundary — an accrual row AT or WITHIN grace is unrepresentable
        (the grace off-by-one can never be persisted);
      - basis / rate / grace bounds mirror the 0017 tenant_settings
        CHECKs the values were read from.

  * uq_penalty_accruals_claim — the UNIQUE claim key doubles as the
    index behind the accrual scan's NOT EXISTS anti-join (gate 1.3:
    the index ships in the same MR as the query; EXPLAIN captured in
    tests/test_p138_explain.py -> backend/perf/explain_p138.txt). The
    driving scan reuses idx_loans_active_scan (0007) unchanged.

  * RLS enabled + FORCED with the 0001 tenant_isolation policy shape
    (ADR-0002); the leakage suite is extended to this table.

Ledger boundary (documented per P13.8): penalty INCOME recognition
stays ON RECEIPT — the P10 repayment allocation posts income.penalties
when cash arrives. This table and the job maintain ONLY the
receivable-side ``loans.penalty_due``; no ledger rows are written here.

Downgrade fully reverses this revision: it drops only the new table
(and its policy/indexes with it) and touches no pre-existing data.
``loans.penalty_due`` values already accrued remain, exactly like any
other money state a reverted feature wrote while live.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE penalty_accruals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    loan_id uuid NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    accrual_date date NOT NULL,
    basis text NOT NULL
        CHECK (basis IN ('instalment_in_arrears', 'full_outstanding')),
    basis_amount numeric(18,2) NOT NULL CHECK (basis_amount >= 0),
    rate_pct_per_month numeric(5,2) NOT NULL
        CHECK (rate_pct_per_month >= 0 AND rate_pct_per_month <= 100),
    grace_days integer NOT NULL
        CHECK (grace_days >= 0 AND grace_days <= 365),
    days_past_due integer NOT NULL,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Grace boundary backstop: an accrual at or within grace is
    -- unrepresentable (P13.8 failure mode 2).
    CONSTRAINT ck_penalty_accruals_past_grace
        CHECK (days_past_due > grace_days),
    -- The atomic idempotency claim (v1.1 rule 5) AND the anti-join
    -- index behind the accrual scan (gate 1.3).
    CONSTRAINT uq_penalty_accruals_claim
        UNIQUE (tenant_id, loan_id, accrual_date)
);

ALTER TABLE penalty_accruals ENABLE ROW LEVEL SECURITY;
ALTER TABLE penalty_accruals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON penalty_accruals
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
DROP TABLE IF EXISTS penalty_accruals;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
