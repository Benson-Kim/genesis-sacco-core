"""dividends & share lifecycle (P13.11)

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-01

Expand-only revision backing BUILD_PROMPTS P13.11: dividend on shares
and deposit rebates (declaration -> committee approval -> distribution)
plus share transfer at exit. RLS enabled + FORCED on every new table
with the 0001/0019 tenant_isolation policy shape (ADR-0002); DB CHECKs
on every bound (gate 1.5); the leakage suite is extended to all four
tables.

  * tenant_settings.deposit_rebate_rate_pct — the deposit-rebate rate
    the declaration reads (P13.7 pattern: nullable column, DB CHECK
    mirroring the registry bounds; the settings API stays the single
    legitimate writer, v1.1 rule 1). A tenant that pays no rebate
    configures 0.00 explicitly — a missing key fails the declaration
    CLOSED (never a defaulted rate).

  * dividend_declarations — the PERSISTED APPROVAL SNAPSHOT (v1.1
    rule 3): financial-year period, the dividend and rebate rates in
    force, eligible-member count, total bases and the exact payout the
    committee approves. Execution re-verifies these components and
    409s on drift, posting nothing. CHECKs pin every bound and the
    internal identity total_payout = total_dividend + total_rebate;
    total_payout > 0 makes an empty declaration unrepresentable.

  * uq_dividend_declarations_fy — at most ONE live (non-rejected)
    declaration per financial year: concurrent double-declarations
    collapse to exactly one row, and a DISTRIBUTED year can never be
    declared again (double-dividend at the year level is
    unrepresentable). Rejected/voided declarations free the slot.

  * dividend_declarations immutability trigger — a declaration is
    WRITE-ONCE on its snapshot columns from the moment it is created
    (fy period, rates, count, bases, totals, requested_by): the
    committee approved THOSE figures, so any later mutation — even
    manual SQL through the app role — must fail loudly (P13.11
    failure mode 4; the 0017 role-rename trigger precedent). Status
    workflow columns (status, decided_at, distributed_at, version,
    updated_at) stay writable for the declared -> approved ->
    distributed transitions.

  * dividend_distributions — the per-(member, declaration) idempotency
    CLAIM (v1.1 rule 5, the 0008/0019 precedent): the UNIQUE key is
    the database-level guarantee that a member is paid at most once
    per declaration; it doubles as the index behind the distribution
    scan's NOT EXISTS anti-join (gate 1.3 — index ships with its
    query). The row records the reconstructed bases, both rates and
    both amounts verbatim, so every posted shilling is reconstructable
    after any config change. total_amount > 0: zero-entitlement
    members are SKIPPED, never 0.00-posted (P13.11 failure mode 5 —
    no noise rows). transaction_id is filled in the same transaction
    as the claim (claim + posting + audit are one atomic unit); it is
    nullable only because the claim insert precedes the posting inside
    that transaction.

  * idx_dividend_distributions_page — keyset page for the dividend &
    rebate schedule report (tenant_id, declaration_id, created_at,
    id), shipped with the report query (gate 1.3).

  * share_transfers — the transfer-at-exit record: transferor,
    transferee (self-transfer unrepresentable by CHECK), amount > 0,
    and BOTH ledger transaction ids (the OUT and IN legs post as two
    member-attributed transactions through a dedicated clearing
    account so each member's ledger-reconstructed share history stays
    exact — see genesis/domain/ledger.py).

  * transactions.type CHECK expanded (expand-only) with
    'dividend_posting', 'share_transfer_out' and 'share_transfer_in'.
    Distinct types keep member statements and the schedule report
    honest (a dividend is not "interest") and keep the WD-/SH- ledger
    taxonomy intact; DV-/ST- reference prefixes are allocated through
    the P7 advisory-lock generator.

Downgrade reverses this revision: the new tables, trigger, function
and settings column are dropped and the original transactions.type
CHECK is restored. Restoring the narrower CHECK VALIDATES existing
rows, so a database that already holds dividend or share-transfer
transactions refuses the downgrade loudly (the 0017 conditional-refusal
precedent — money history is never silently deleted); CI's
migrate-check (up -> down -> up) runs on a database without such rows
and passes.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 0. Deposit-rebate rate (P13.7 pattern: registry-declared, CHECK-mirrored)
-- ---------------------------------------------------------------------------
ALTER TABLE tenant_settings
    ADD COLUMN deposit_rebate_rate_pct numeric(5,2)
        CHECK (deposit_rebate_rate_pct >= 0 AND deposit_rebate_rate_pct <= 100);

-- ---------------------------------------------------------------------------
-- 1. Dividend declarations: the persisted approval snapshot (v1.1 rule 3)
-- ---------------------------------------------------------------------------
CREATE TABLE dividend_declarations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    fy_start date NOT NULL,
    fy_end date NOT NULL CHECK (fy_end > fy_start),
    dividend_rate_pct numeric(5,2) NOT NULL
        CHECK (dividend_rate_pct >= 0 AND dividend_rate_pct <= 100),
    rebate_rate_pct numeric(5,2) NOT NULL
        CHECK (rebate_rate_pct >= 0 AND rebate_rate_pct <= 100),
    eligible_members integer NOT NULL CHECK (eligible_members > 0),
    total_share_basis numeric(18,2) NOT NULL CHECK (total_share_basis >= 0),
    total_deposit_basis numeric(18,2) NOT NULL CHECK (total_deposit_basis >= 0),
    total_dividend numeric(18,2) NOT NULL CHECK (total_dividend >= 0),
    total_rebate numeric(18,2) NOT NULL CHECK (total_rebate >= 0),
    total_payout numeric(18,2) NOT NULL CHECK (total_payout > 0),
    status text NOT NULL DEFAULT 'declared'
        CHECK (status IN ('declared', 'approved', 'rejected', 'distributed')),
    requested_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    decided_at timestamptz,
    distributed_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- The snapshot's internal identity: the payout IS the sum of its
    -- parts, per-member figures having been rounded exactly once each
    -- (the documented residue rule: totals are DERIVED from the
    -- rounded per-member figures, never allocated down from a pot).
    CONSTRAINT ck_dividend_declarations_totals
        CHECK (total_payout = total_dividend + total_rebate)
);

-- One live declaration per financial year (concurrent double-declares
-- collapse; a distributed year is permanently closed).
CREATE UNIQUE INDEX uq_dividend_declarations_fy
    ON dividend_declarations (tenant_id, fy_start)
    WHERE status <> 'rejected';

-- Listing keyset (newest first), the member_exits 0010 shape.
CREATE INDEX idx_dividend_declarations_keyset
    ON dividend_declarations (tenant_id, created_at DESC, id DESC);

-- Write-once snapshot columns (failure mode 4): the figures the
-- committee votes on can never be edited after creation, in ANY
-- status -- only the status workflow columns may move (a drifted
-- snapshot is voided and redeclared, never edited). DB-enforced so
-- even manual SQL through the app role fails loudly (0017 precedent).
CREATE OR REPLACE FUNCTION forbid_dividend_snapshot_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF (NEW.fy_start, NEW.fy_end, NEW.dividend_rate_pct, NEW.rebate_rate_pct,
        NEW.eligible_members, NEW.total_share_basis, NEW.total_deposit_basis,
        NEW.total_dividend, NEW.total_rebate, NEW.total_payout,
        NEW.requested_by)
       IS DISTINCT FROM
       (OLD.fy_start, OLD.fy_end, OLD.dividend_rate_pct, OLD.rebate_rate_pct,
        OLD.eligible_members, OLD.total_share_basis, OLD.total_deposit_basis,
        OLD.total_dividend, OLD.total_rebate, OLD.total_payout,
        OLD.requested_by)
    THEN
        RAISE EXCEPTION
            'dividend declaration % snapshot is write-once: void and redeclare',
            OLD.id;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER dividend_declarations_write_once
    BEFORE UPDATE ON dividend_declarations
    FOR EACH ROW EXECUTE FUNCTION forbid_dividend_snapshot_mutation();

ALTER TABLE dividend_declarations ENABLE ROW LEVEL SECURITY;
ALTER TABLE dividend_declarations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dividend_declarations
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 2. Committee votes on a declaration (the P9/0005/0010 voting shape)
-- ---------------------------------------------------------------------------
CREATE TABLE dividend_declaration_votes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    declaration_id uuid NOT NULL
        REFERENCES dividend_declarations(id) ON DELETE RESTRICT,
    voter_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    vote text NOT NULL CHECK (vote IN ('approve', 'reject')),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Double-voting impossible at the database level (gate 1.4).
    UNIQUE (tenant_id, declaration_id, voter_id)
);

ALTER TABLE dividend_declaration_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE dividend_declaration_votes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dividend_declaration_votes
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 3. Distribution claims: at most one payout per (member, declaration)
-- ---------------------------------------------------------------------------
CREATE TABLE dividend_distributions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    declaration_id uuid NOT NULL
        REFERENCES dividend_declarations(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    share_basis numeric(18,2) NOT NULL CHECK (share_basis >= 0),
    deposit_basis numeric(18,2) NOT NULL CHECK (deposit_basis >= 0),
    dividend_rate_pct numeric(5,2) NOT NULL
        CHECK (dividend_rate_pct >= 0 AND dividend_rate_pct <= 100),
    rebate_rate_pct numeric(5,2) NOT NULL
        CHECK (rebate_rate_pct >= 0 AND rebate_rate_pct <= 100),
    dividend_amount numeric(18,2) NOT NULL CHECK (dividend_amount >= 0),
    rebate_amount numeric(18,2) NOT NULL CHECK (rebate_amount >= 0),
    -- No noise rows (failure mode 5): a zero entitlement is skipped,
    -- never claimed; and the row's own figures must add up.
    total_amount numeric(18,2) NOT NULL CHECK (total_amount > 0),
    transaction_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_dividend_distributions_total
        CHECK (total_amount = dividend_amount + rebate_amount),
    -- The atomic idempotency claim (v1.1 rule 5) AND the index behind
    -- the distribution scan's NOT EXISTS anti-join (gate 1.3).
    CONSTRAINT uq_dividend_distributions_claim
        UNIQUE (tenant_id, declaration_id, member_id)
);

-- Keyset page for the dividend & rebate schedule report (gate 1.3:
-- the index ships in the same MR as the report query).
CREATE INDEX idx_dividend_distributions_page
    ON dividend_distributions (tenant_id, declaration_id, created_at, id);

CREATE INDEX idx_dividend_distributions_txn
    ON dividend_distributions (tenant_id, transaction_id);

ALTER TABLE dividend_distributions ENABLE ROW LEVEL SECURITY;
ALTER TABLE dividend_distributions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON dividend_distributions
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 4. The distribution scan's driving index (gate 1.3)
-- ---------------------------------------------------------------------------
-- Keyset walk over the eligible population (active/arrears members) in
-- id order; partial so exited members never widen the scan.
CREATE INDEX idx_members_dividend_scan
    ON members (tenant_id, id)
    WHERE status IN ('active', 'arrears');

-- ---------------------------------------------------------------------------
-- 5. Share transfers at exit
-- ---------------------------------------------------------------------------
CREATE TABLE share_transfers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    from_member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    to_member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    out_transaction_id uuid NOT NULL
        REFERENCES transactions(id) ON DELETE RESTRICT,
    in_transaction_id uuid NOT NULL
        REFERENCES transactions(id) ON DELETE RESTRICT,
    created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Self-transfer is unrepresentable (gate 1.5).
    CONSTRAINT ck_share_transfers_distinct_members
        CHECK (from_member_id <> to_member_id)
);

CREATE INDEX idx_share_transfers_from
    ON share_transfers (tenant_id, from_member_id, created_at DESC, id DESC);
CREATE INDEX idx_share_transfers_to
    ON share_transfers (tenant_id, to_member_id, created_at DESC, id DESC);

ALTER TABLE share_transfers ENABLE ROW LEVEL SECURITY;
ALTER TABLE share_transfers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON share_transfers
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 6. Transaction taxonomy: dividend + share-transfer types (expand-only)
-- ---------------------------------------------------------------------------
ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN
        ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
         'loan_repayment', 'interest_posting', 'exit_settlement',
         'dividend_posting', 'share_transfer_out', 'share_transfer_in'));

-- The 0013 exports.report CHECK pins the report vocabulary; the P13
-- registry gains the dividend & rebate schedule here (expand-only).
ALTER TABLE exports DROP CONSTRAINT exports_report_check;
ALTER TABLE exports ADD CONSTRAINT exports_report_check
    CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement',
         'dividend_rebate_schedule'));
"""

_DOWN = """
ALTER TABLE exports DROP CONSTRAINT exports_report_check;
ALTER TABLE exports ADD CONSTRAINT exports_report_check
    CHECK (report IN
        ('member_statement', 'trial_balance', 'loan_book',
         'disbursement_collections', 'npl_trend', 'member_exit_statement'));

ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
-- Restoring the narrower CHECK validates existing rows: a database
-- holding dividend/share-transfer history refuses this loudly (0017
-- conditional-refusal precedent) instead of stranding invalid rows.
ALTER TABLE transactions ADD CONSTRAINT transactions_type_check
    CHECK (type IN
        ('deposit', 'withdrawal', 'share_topup', 'loan_disbursement',
         'loan_repayment', 'interest_posting', 'exit_settlement'));

DROP TABLE IF EXISTS share_transfers;
DROP INDEX IF EXISTS idx_members_dividend_scan;
DROP TABLE IF EXISTS dividend_distributions;
DROP TABLE IF EXISTS dividend_declaration_votes;
DROP TRIGGER IF EXISTS dividend_declarations_write_once ON dividend_declarations;
DROP FUNCTION IF EXISTS forbid_dividend_snapshot_mutation();
DROP TABLE IF EXISTS dividend_declarations;

ALTER TABLE tenant_settings DROP COLUMN deposit_rebate_rate_pct;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
