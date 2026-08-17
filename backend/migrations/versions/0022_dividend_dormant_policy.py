"""dividends x dormancy policy (issue #19)

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-01

Backs the issue-#19 maintainer policy decision (P13.11 x P13.13).
Claimed as 0022 with down_revision 0021 per v1.2 rule 14 — 0021
(member dormancy, !32) verified as main's migration head at branch
time (0001-0021 linear).

  * idx_members_dividend_scan predicate WIDENED to include 'dormant'
    (issue #19 P1: dormant members remain shareholders and ARE
    dividend/rebate-eligible on their share basis; exited members
    stay excluded from direct payment). The swap is CREATE new + DROP
    old under a rename, atomic inside the migration transaction, and
    keeps the index name the production scan and the EXPLAIN gate
    (tests/test_p1311_explain.py) assert against. The predicate
    matches application/dividends.members_scan_sql exactly (partial-
    index implication), shipped in the same MR as the widened query
    (gate 1.3).

  * dividend_distributions.disposition — the explicit, audited
    terminal disposition on the per-member claim row (issue #19 P3):
    'paid' (the !30 path: posted and credited to the member's
    accounts) or 'unclaimed' (a member who EXITED mid-run: their
    record-date entitlement is recognised as an explicit
    liability.unclaimed_dividends payable posting, never credited to
    member accounts and never left as a silent pending_members
    shortfall). Additive, NOT NULL DEFAULT 'paid': every existing !30
    claim row IS a paid disposition. Resolution of unclaimed rows
    happens through the P13.15 correction paths (reversing entries)
    — a forward reference, deliberately not modelled here.

  * idx_members_exited_scan — the driving index for the unclaimed
    disposition scan (application/dividends.unclaimed_scan_sql,
    shipped with this query, gate 1.3). Partial over
    status = 'exited'; the member_exits settled-after-declaration
    probe rides idx_exits_member (0001) and the claim anti-join rides
    uq_dividend_distributions_claim (0020).

No new tables: no TENANT_TABLES delta, no new RLS policies
(dividend_distributions keeps its 0020 forced tenant_isolation
policy), no ENTITY_MODULES change (unclaimed audit rows use
entity='dividend_distributions', already mapped). No new lock-graph
edges (the disposition scan locks member rows only — the ROOT tier of
the established chain, exactly like the !30 batch scan).

Downgrade restores the narrower active/arrears scan predicate cleanly
and drops the disposition column — but REFUSES LOUDLY on a database
holding 'unclaimed' dispositions (the 0017/0020/0021 conditional-
refusal precedent): an unclaimed disposition is money-bearing audit
state (a recognised payable) and is never silently rewritten to
'paid'. Resolve unclaimed rows through the P13.15 correction paths
first. CI migrate-check (up -> down -> up) runs on a database without
unclaimed rows and passes.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_UP = """
-- 1. Widen the dividend-scan population to dormant members (issue #19
--    P1). CREATE new + DROP old via rename: the swap is atomic inside
--    the migration transaction and preserves the asserted index name.
ALTER INDEX idx_members_dividend_scan RENAME TO idx_members_dividend_scan_prev;
CREATE INDEX idx_members_dividend_scan
    ON members (tenant_id, id)
    WHERE status IN ('active', 'arrears', 'dormant');
DROP INDEX idx_members_dividend_scan_prev;

-- 2. Explicit per-claim disposition (issue #19 P3). Expand-only:
--    every pre-existing claim row is a paid disposition.
ALTER TABLE dividend_distributions
    ADD COLUMN disposition text NOT NULL DEFAULT 'paid'
        CONSTRAINT ck_dividend_distributions_disposition
        CHECK (disposition IN ('paid', 'unclaimed'));

-- 3. Driving index for the unclaimed disposition scan (gate 1.3:
--    shipped in the same migration as its query,
--    application/dividends.unclaimed_scan_sql).
CREATE INDEX idx_members_exited_scan
    ON members (tenant_id, id)
    WHERE status = 'exited';
"""

_DOWN = """
-- An 'unclaimed' disposition is a recognised payable: dropping the
-- column would silently rewrite money-disposition state to 'paid'.
-- Refuse loudly (the 0017/0020/0021 precedent); resolve unclaimed
-- rows via the P13.15 correction paths first.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM dividend_distributions WHERE disposition = 'unclaimed') THEN
        RAISE EXCEPTION 'refusing downgrade: unclaimed dividend dispositions exist; '
            'resolve them through the correction paths before downgrading';
    END IF;
END
$$;

DROP INDEX IF EXISTS idx_members_exited_scan;

ALTER TABLE dividend_distributions DROP COLUMN disposition;

ALTER INDEX idx_members_dividend_scan RENAME TO idx_members_dividend_scan_prev;
CREATE INDEX idx_members_dividend_scan
    ON members (tenant_id, id)
    WHERE status IN ('active', 'arrears');
DROP INDEX idx_members_dividend_scan_prev;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
