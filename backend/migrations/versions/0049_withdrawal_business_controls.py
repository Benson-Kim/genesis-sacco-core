"""withdrawal business controls: daily velocity cap + notice holds (issue #2)

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-19

Chained onto the current single head 0048 (verified: no other revision
claims 0048 as its parent — the issue-#22 re-chain-not-merge-revision
rule; never a merge revision).

Expand-only revision backing the issue-#2 business-control tranche:

  1. tenant_settings gains two nullable money columns (the 0017
     convention: NULL = "not configured", consumers fall back to their
     code-owned default — here: no cap / no threshold):
       * daily_withdrawal_limit — per-member DAILY withdrawal velocity
         cap. Enforced server-side inside the existing withdrawal
         chain (member FOR SHARE -> deposit account FOR UPDATE), so
         the cap read and the posting are one atomic unit — no TOCTOU.
       * withdrawal_notice_threshold — withdrawals strictly above the
         threshold do not execute immediately: they enter a
         pending_notice hold (table below) with an outbox notification
         event, per the existing state-machine conventions.
     DB CHECKs pin the bounds exactly like every other 0017 key.

  2. withdrawal_holds — the notice-state ledger for large
     withdrawals. Status machine is code-owned
     (genesis/domain/withdrawals.py, the lending.transition
     convention): pending_notice -> executed | cancelled, both
     terminal. DB constraints, not just app validation:
       * status CHECK pins the vocabulary;
       * amount > 0 (the transactions convention);
       * channel restricted to the two cash channels the teller
         boundary accepts (require_cash_channel mirror);
       * (status = 'executed') = (executed_txn_id IS NOT NULL) — an
         executed hold ALWAYS points at its posting and only an
         executed hold may;
       * (status = 'pending_notice') = (decided_at IS NULL) — a
         decided hold always carries its decision time.
     Optimistic locking via version (409 on stale, the house
     convention); keyset-index (tenant_id, status, created_at DESC,
     id DESC) serves the status-filtered list newest-first.

  3. RLS: enabled AND forced with the 0001 tenant_isolation policy —
     the new table joins every other tenant table behind ADR-0002.

Downgrade is guarded (the 0017 refusal convention): it REFUSES while
any withdrawal_holds row exists or any tenant has configured either
new setting — those rows/values are money controls, never silently
dropped. CI's migrate-check (up -> down -> up) runs on an empty
database and is unaffected.
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. Tenant-configurable withdrawal business controls (0017 convention)
-- ---------------------------------------------------------------------------
ALTER TABLE tenant_settings
    ADD COLUMN daily_withdrawal_limit numeric(18,2)
        CHECK (daily_withdrawal_limit > 0),
    ADD COLUMN withdrawal_notice_threshold numeric(18,2)
        CHECK (withdrawal_notice_threshold > 0);

-- ---------------------------------------------------------------------------
-- 2. Notice holds for large withdrawals (pending state, issue #2)
-- ---------------------------------------------------------------------------
CREATE TABLE withdrawal_holds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    channel text NOT NULL CHECK (channel IN ('mpesa', 'bank')),
    external_ref text,
    status text NOT NULL DEFAULT 'pending_notice'
        CHECK (status IN ('pending_notice', 'executed', 'cancelled')),
    threshold_at_request numeric(18,2) NOT NULL
        CHECK (threshold_at_request > 0),
    requested_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    decided_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    executed_txn_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    decided_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'executed') = (executed_txn_id IS NOT NULL)),
    CHECK ((status = 'pending_notice') = (decided_at IS NULL))
);
CREATE INDEX idx_withdrawal_holds_status_keyset
    ON withdrawal_holds (tenant_id, status, created_at DESC, id DESC);
CREATE INDEX idx_withdrawal_holds_member
    ON withdrawal_holds (tenant_id, member_id, created_at);
CREATE INDEX idx_withdrawal_holds_txn
    ON withdrawal_holds (tenant_id, executed_txn_id)
    WHERE executed_txn_id IS NOT NULL;

ALTER TABLE withdrawal_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE withdrawal_holds FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON withdrawal_holds
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN_GUARD = """
DO $guard$
DECLARE
    holds bigint;
    configured bigint;
BEGIN
    SELECT count(*) INTO holds FROM withdrawal_holds;
    SELECT count(*) INTO configured FROM tenant_settings
        WHERE daily_withdrawal_limit IS NOT NULL
           OR withdrawal_notice_threshold IS NOT NULL;
    IF holds > 0 OR configured > 0 THEN
        RAISE EXCEPTION
            'downgrade refused: % withdrawal_holds row(s) and % configured '
            'tenant(s) carry withdrawal business controls. Export/settle the '
            'holds and clear the two settings via PUT /settings first '
            '(the 0017 non-destructive downgrade convention).',
            holds, configured;
    END IF;
END
$guard$;
"""

_DOWN = """
DROP TABLE withdrawal_holds;
ALTER TABLE tenant_settings
    DROP COLUMN daily_withdrawal_limit,
    DROP COLUMN withdrawal_notice_threshold;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN)
