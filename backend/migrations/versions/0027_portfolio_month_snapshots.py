"""month-end portfolio snapshots (.17a / DSA-1)

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-02

Claimed as 0027 with down_revision 0026 per the migration-declaration rule. At branch
time main's migration head was 0025 and **0026 is the open claim of
 ** — this MR therefore merges AFTER and its DB-backed
jobs are structurally blocked until 0026 lands on main (sequencing declared in the MR description;
combined-state pipeline re-verified per the house rules before ready). If closes instead of merging,
this
revision re-chains to 0025 (the 0017 re-chain discipline).

One expand-only object backing (a):

  * portfolio_month_snapshots — the persisted month-end portfolio
    figures (gross outstanding, NPL balance, loan counts) that the
    NPL-trend export previously RECONSTRUCTED from the append-only
    record for every configured month on every export (DSA-1: months x
    full-history rescans). A snapshot row is written exactly once when
    a month completes — at close_period under its exclusive per-tenant
    advisory barrier, or by the backfill job — using the SAME
    reconstruction statement the export used
    (application/portfolio_reconstruction.NPL_TREND_MONTH_SQL — single source of truth,
    reuse-first), so the stored figures are BY
    CONSTRUCTION the figures a full rescan would produce.

  * WRITE-ONCE at the database level (the 0020-trigger precedent): these are the figures auditors
  and regulators read —
    a restated month is the classic concealment vector — so UPDATE and
    DELETE are refused by trigger for EVERY role subject to triggers,
    including manual SQL through the app role. A wrong snapshot is
    investigated and re-established through migration-level tooling
    under review, never edited in place. Divergence between an
    existing snapshot and a fresh reconstruction 409s loudly at the
    write sites (close_period / backfill) and never self-heals.

  * Insider hardening (DB level, not app checks): RLS enabled AND
    FORCED with the 0001 tenant_isolation policy (ADR-0002); CHECKs
    pin every bound — counts and balances non-negative, the NPL subset
    never exceeding the portfolio total, and month_end pinned to a
    real calendar month end so a fabricated mid-month "snapshot" is
    unrepresentable; a BEFORE INSERT fence refuses any
    month that has not fully elapsed - a fabricated FUTURE month would
    otherwise be frozen undeletable by the write-once trigger and
    permanently 409 that month's real close (a CHECK cannot carry the
    rule: now() is not immutable). UNIQUE (tenant_id, month_end) makes
    concurrent
    writers collapse to exactly one row (claimed atomically, v1.1
    rule 5) and serves the export's snapshot lookup (scalability: the index ships with the query it
    serves, application/portfolio_snapshots.SNAPSHOT_LOOKUP_SQL).

Lock posture (0024 honesty precedent): CREATE TABLE takes no lock on
existing relations; no index is added to an existing table. CREATE
FUNCTION/TRIGGER on the NEW table locks nothing in use.

Downgrade REFUSES LOUDLY while snapshot rows exist (the 0017/0020
conditional-refusal precedent): month-end portfolio figures are
money-bearing regulatory history and are never silently deleted. CI's
migrate-check (up -> down -> up) runs on an empty database and passes.
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE portfolio_month_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    month_end date NOT NULL,
    loans integer NOT NULL CHECK (loans >= 0),
    gross_outstanding numeric(18,2) NOT NULL CHECK (gross_outstanding >= 0),
    npl_loans integer NOT NULL CHECK (npl_loans >= 0),
    npl_balance numeric(18,2) NOT NULL CHECK (npl_balance >= 0),
    source text NOT NULL CHECK (source IN ('close_period', 'backfill')),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- The NPL subset can never exceed the whole portfolio: the
    -- reconstruction sums non-negative per-loan outstandings, so a
    -- row violating these is fabricated, not computed (gate 1.5).
    CONSTRAINT ck_portfolio_snapshots_npl_loans CHECK (npl_loans <= loans),
    CONSTRAINT ck_portfolio_snapshots_npl_balance
        CHECK (npl_balance <= gross_outstanding),
    -- A snapshot exists only for a real month END: a mid-month row is
    -- unrepresentable (insider hardening — a direct-SQL INSERT cannot
    -- fabricate a cutoff the reconstruction would never use).
    CONSTRAINT ck_portfolio_snapshots_month_end CHECK (
        month_end = (date_trunc('month', month_end)
                     + interval '1 month - 1 day')::date
    ),
    -- One snapshot per month, claimed atomically (v1.1 rule 5); also
    -- the export's lookup index (gate 1.3).
    CONSTRAINT uq_portfolio_snapshots_month UNIQUE (tenant_id, month_end)
);

-- WRITE-ONCE (P13.17 FM1, the 0020 precedent): a restated month is
-- unrepresentable at the database level. Corrections to the underlying
-- record post as reversing entries in the OPEN period (0012 contract);
-- closed-month figures are history, not state.
CREATE FUNCTION forbid_portfolio_snapshot_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION
        'portfolio_month_snapshots is write-once: month % may never be '
        'updated or deleted (P13.17 FM1)', OLD.month_end;
END
$fn$;

CREATE TRIGGER portfolio_month_snapshots_write_once
    BEFORE UPDATE OR DELETE ON portfolio_month_snapshots
    FOR EACH ROW EXECUTE FUNCTION forbid_portfolio_snapshot_mutation();

-- FUTURE-MONTH FENCE (review !49 N1): only FULLY ELAPSED months are
-- representable. Without this, a fabricated future-month row passes
-- every CHECK, the write-once trigger then freezes it undeletable,
-- and the month's REAL close 409s forever on verify-on-conflict - an
-- irreversible denial-of-integrity vector needing a reviewed
-- migration to clear. A CHECK constraint cannot express this fence
-- (now() is not immutable), so it is a BEFORE INSERT trigger, binding
-- for every trigger-subject role including direct SQL through the
-- app role. month_end >= date_trunc('month', now())::date refuses the
-- current month's end and everything later; the last fully elapsed
-- month stays representable (the writer's own fully-elapsed rule,
-- now enforced at the database).
CREATE FUNCTION forbid_future_portfolio_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.month_end >= date_trunc('month', now())::date THEN
        RAISE EXCEPTION
            'portfolio_month_snapshots: month % has not fully elapsed; only '
            'fully elapsed months are representable (P13.17 N1)',
            NEW.month_end;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER portfolio_month_snapshots_no_future
    BEFORE INSERT ON portfolio_month_snapshots
    FOR EACH ROW EXECUTE FUNCTION forbid_future_portfolio_snapshot();

ALTER TABLE portfolio_month_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE portfolio_month_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON portfolio_month_snapshots
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
-- Conditional refusal (0017/0020 precedent): snapshot rows are
-- money-bearing regulatory history; dropping them silently is the
-- restatement vector this table exists to close. Export and archive
-- them deliberately before downgrading.
DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM portfolio_month_snapshots) THEN
        RAISE EXCEPTION
            'downgrade refused: portfolio_month_snapshots holds month-end '
            'portfolio history; archive and delete it deliberately first '
            '(P13.17a / 0017 conditional-refusal precedent)';
    END IF;
END
$guard$;

DROP TRIGGER IF EXISTS portfolio_month_snapshots_no_future
    ON portfolio_month_snapshots;
DROP FUNCTION IF EXISTS forbid_future_portfolio_snapshot();
DROP TRIGGER IF EXISTS portfolio_month_snapshots_write_once
    ON portfolio_month_snapshots;
DROP FUNCTION IF EXISTS forbid_portfolio_snapshot_mutation();
DROP TABLE IF EXISTS portfolio_month_snapshots;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
