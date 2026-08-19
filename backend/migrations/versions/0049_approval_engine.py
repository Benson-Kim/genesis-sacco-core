"""approval engine: effective-dated band sets + pending approvals (G2)

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-19

Expand-only revision backing ADR-0008 (issue #8, gap register G2): the
limits/approval engine's schema — maker-checker with amount-tiered,
tenant-configurable, EFFECTIVE-DATED approval bands.

Sequencing (issue-#22 governance): claimed as 0049 with down_revision
'0048' — the single verified head of develop at branch time. Other
in-flight branches carry their own 0049 claims (the EOD-reconciliation
and withdrawal-controls tracks); whichever merges after the first must
RE-CHAIN its unreleased revision onto the new head (edit down_revision
AND this prose — the 0017 precedent), NEVER add a multi-parent merge
revision (the 0049-merge-revision approach failed migrate-check's
`alembic downgrade -1` with "Ambiguous walk").

  * approval_band_sets — one APPEND-ONLY row per (tenant,
    effective_from): the band matrix in force FROM that date (the
    issue-#3 effective-dated regulatory-parameter discipline). A
    tenant reconfigures limits by appending a later effective date,
    never by editing history — "which bands were in force on date D"
    stays answerable forever, so a pending approval can always be
    re-resolved against the bands of its request day
    (stricter-of-the-two, ADR-0008). The DB refuses UPDATE and DELETE
    outright (trigger below); the (tenant_id, effective_from) UNIQUE
    is the atomic claim key (INSERT ... ON CONFLICT DO NOTHING +
    rowcount, v1.1 rule 5). bands is a jsonb array validated by the
    shared write/read contract (domain/tenant_config.
    validate_approval_bands) at the application layer; the DB CHECK
    pins the top-level type (the 0017 tenant_settings precedent — a
    CHECK cannot express the band contract, so reads REVALIDATE and
    fail closed). No band values are duplicated into the DB as
    constraints: the day-one defaults (Loan Officer <= 100k, Branch
    Manager <= 500k, Credit Committee <= 2M, Board above) live in
    domain/approvals.DEFAULT_APPROVAL_BANDS and apply only when a
    tenant has NO rows here — the 0026 classification-ladder-mirror
    lesson applied by construction.

  * pending_approvals — one write-once workflow row per above-band
    operation: (operation type, amount) declared by the maker, the
    required tier snapshot at request time, and the eventual decision
    by a DIFFERENT principal.
    - DB-LEVEL SEGREGATION OF DUTIES (the 0031/0040 discipline):
      ck_pending_approvals_sod — checker_id IS NULL OR
      checker_id <> maker_id. The server-side guard
      (application/sod.require_distinct_non_assurance_checker) refuses
      first; this CHECK makes a maker-ratified-own row unrepresentable
      even through direct SQL on the app role. (The assurance-role
      exclusion stays server-side — role NAMES are invisible to a
      CHECK.)
    - status machine at the database: 'pending' -> 'ratified' |
      'declined', enforced by the write-once trigger below; a decided
      row always carries checker_id AND decided_at, a pending row
      never does (ck_pending_approvals_decided).
    - write-once (the 0031 pattern): identity/money columns
      (tenant/operation/amount/branch/maker/tier/requested_at) are
      pinned after INSERT; checker_id / decided_at / decision_reason
      fill exactly once; DELETE is refused — pending-approval rows are
      workflow history like the corrections trail.
    - branch_id (nullable FK): the operation's branch — branch-scoping
      groundwork (staff carry a home branch per 0016; cross-branch
      action becomes a NAMED permission in the wiring MR, never a
      default).
    - amount is NUMERIC(18,2) CHECK (amount > 0): the engine gates
      money-bearing operations only.

  * transactions.checked_by — the SECOND principal on a ratified
    posting, extending the 0036 created_by attribution: the wired
    executor records maker (created_by) and checker (checked_by) on
    the posting itself. NULL stays the honest "no checker" (system
    jobs, below-band single-actor postings) — never a fabricated
    principal. The 0004 append-only fence pins the column the moment
    the row commits; like 0036, NO index ships (no read path filters
    by checked_by; the write-heaviest table is not taxed for a column
    nothing scans). No backfill: no historical posting ever had a
    checker — inventing one would be forgery.

  * Indexes shipped with the queries that need them (gate 1.3/1.5):
    every FK is indexed; idx_pending_approvals_open (tenant_id,
    requested_at, id) WHERE status = 'pending' serves the wiring MR's
    approval worklist keyset scan.

  * RLS enabled AND FORCED with the 0001 tenant_isolation policy shape
    (ADR-0002); both tables join TENANT_TABLES and the leakage suite
    in this MR.

Downgrade REFUSES LOUDLY (the 0017/0020/0031 precedent) when any
approval history exists: configured band schedules are the record of
WHICH limits were in force when, pending_approvals rows are
maker-checker workflow history, and a non-NULL transactions.checked_by
is the checker attribution itself — silently dropping any of them
would recreate the very G2 control gap this revision closes, as data
loss. On a clean expansion (migrate-check's up -> down -> up) it
reverses exactly.
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. approval_band_sets: tenant-scoped, effective-dated, APPEND-ONLY
-- ---------------------------------------------------------------------------
CREATE TABLE approval_band_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    effective_from date NOT NULL,
    bands jsonb NOT NULL CHECK (jsonb_typeof(bands) = 'array'),
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- The atomic claim key: one matrix per effective date, appended
    -- with INSERT ... ON CONFLICT DO NOTHING + rowcount (v1.1 rule 5).
    CONSTRAINT uq_approval_band_sets_effective UNIQUE (tenant_id, effective_from)
);

-- FK index (gate 1.5); the UNIQUE above doubles as the
-- effective-date resolution probe (tenant_id, effective_from).
CREATE INDEX idx_approval_band_sets_created_by
    ON approval_band_sets (tenant_id, created_by);

-- Append-only fence: band history is never rewritten or dropped —
-- a reconfiguration is a NEW effective date (ADR-0008).
CREATE FUNCTION forbid_approval_band_set_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION
        'approval_band_sets is append-only (ADR-0008/issue #8): configure a '
        'new effective date instead of rewriting band history';
END
$fn$;

CREATE TRIGGER approval_band_sets_append_only
    BEFORE UPDATE OR DELETE ON approval_band_sets
    FOR EACH ROW EXECUTE FUNCTION forbid_approval_band_set_mutation();

ALTER TABLE approval_band_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_band_sets FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON approval_band_sets
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 2. pending_approvals: the maker-checker workflow row (write-once)
-- ---------------------------------------------------------------------------
CREATE TABLE pending_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    operation_type text NOT NULL
        CHECK (length(operation_type) BETWEEN 1 AND 100),
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    branch_id uuid REFERENCES branches(id) ON DELETE RESTRICT,
    maker_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    checker_id uuid REFERENCES users(id) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ratified', 'declined')),
    required_tier_at_request integer NOT NULL
        CHECK (required_tier_at_request >= 0),
    decision_reason text
        CHECK (decision_reason IS NULL
               OR length(decision_reason) BETWEEN 1 AND 2000),
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    -- DB-level segregation of duties (ADR-0008, the 0031/0040
    -- discipline): a maker can never be their own checker, even via
    -- direct SQL on the app role.
    CONSTRAINT ck_pending_approvals_sod
        CHECK (checker_id IS NULL OR checker_id <> maker_id),
    -- A decided row always records who and when; a pending row never.
    CONSTRAINT ck_pending_approvals_decided
        CHECK (((status = 'pending') = (decided_at IS NULL))
               AND ((status = 'pending') = (checker_id IS NULL)))
);

-- FK indexes (gate 1.5) + the wiring MR's approval worklist keyset.
CREATE INDEX idx_pending_approvals_maker
    ON pending_approvals (tenant_id, maker_id);
CREATE INDEX idx_pending_approvals_checker
    ON pending_approvals (tenant_id, checker_id);
CREATE INDEX idx_pending_approvals_branch
    ON pending_approvals (tenant_id, branch_id);
CREATE INDEX idx_pending_approvals_open
    ON pending_approvals (tenant_id, requested_at, id)
    WHERE status = 'pending';

-- Write-once trigger (the 0031 discipline): pinned identity/money
-- columns, one-shot NULL -> value decision fills, the status machine
-- enforced at the database, and NO DELETE — approval rows are
-- workflow history.
CREATE FUNCTION forbid_pending_approval_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'pending approval % is workflow history and cannot be deleted '
            '(ADR-0008/issue #8)', OLD.id;
    END IF;
    -- Pinned after INSERT: identity, money, and the tier snapshot.
    IF (NEW.tenant_id, NEW.operation_type, NEW.amount, NEW.branch_id,
        NEW.maker_id, NEW.required_tier_at_request, NEW.requested_at)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.operation_type, OLD.amount, OLD.branch_id,
        OLD.maker_id, OLD.required_tier_at_request, OLD.requested_at)
    THEN
        RAISE EXCEPTION
            'pending approval % is write-once (ADR-0008/issue #8)', OLD.id;
    END IF;
    -- One-shot NULL -> value: the decision fills each exactly once.
    IF (OLD.checker_id IS NOT NULL
        AND NEW.checker_id IS DISTINCT FROM OLD.checker_id)
       OR (OLD.decided_at IS NOT NULL
           AND NEW.decided_at IS DISTINCT FROM OLD.decided_at)
       OR (OLD.decision_reason IS NOT NULL
           AND NEW.decision_reason IS DISTINCT FROM OLD.decision_reason)
    THEN
        RAISE EXCEPTION
            'pending approval % decision fields are write-once '
            '(ADR-0008/issue #8)', OLD.id;
    END IF;
    -- The status machine at the database: the ONLY legal move is
    -- pending -> ratified | declined; terminal states are terminal.
    IF NEW.status IS DISTINCT FROM OLD.status
       AND (OLD.status IS DISTINCT FROM 'pending'
            OR NEW.status NOT IN ('ratified', 'declined'))
    THEN
        RAISE EXCEPTION
            'pending approval % status cannot move ''%'' -> ''%'' '
            '(ADR-0008/issue #8)', OLD.id, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER pending_approvals_write_once
    BEFORE UPDATE OR DELETE ON pending_approvals
    FOR EACH ROW EXECUTE FUNCTION forbid_pending_approval_mutation();

ALTER TABLE pending_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON pending_approvals
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- ---------------------------------------------------------------------------
-- 3. Both principals on the posting: checked_by beside 0036 created_by
-- ---------------------------------------------------------------------------
ALTER TABLE transactions
    ADD COLUMN checked_by uuid REFERENCES users(id) ON DELETE RESTRICT;
"""

#: The loud-refusal guard, exposed separately so the falsifiability
#: test can execute it against a database holding approval history
#: (the 0017/0031 _DOWN_GUARD precedent).
_DOWN_GUARD = """
DO $guard$
DECLARE
    band_schedules bigint;
    approval_rows bigint;
    checked_postings bigint;
BEGIN
    SELECT count(*) INTO band_schedules FROM approval_band_sets;
    SELECT count(*) INTO approval_rows FROM pending_approvals;
    SELECT count(*) INTO checked_postings
        FROM transactions WHERE checked_by IS NOT NULL;
    IF band_schedules > 0 OR approval_rows > 0 OR checked_postings > 0 THEN
        RAISE EXCEPTION USING MESSAGE = format(
            'refusing downgrade 0049: approval-engine history exists '
            '(%s band schedules, %s pending-approval rows, %s postings '
            'with checker attribution). Dropping WHICH limits were in '
            'force, WHO requested above-band operations or WHO checked '
            'a posting would recreate the issue-#8 G2 control gap as '
            'data loss (the 0017/0020/0031 refusal precedent).',
            band_schedules, approval_rows, checked_postings);
    END IF;
END
$guard$;
"""

_DOWN = (
    _DOWN_GUARD
    + """
ALTER TABLE transactions DROP COLUMN IF EXISTS checked_by;
DROP TABLE IF EXISTS pending_approvals;
DROP FUNCTION IF EXISTS forbid_pending_approval_mutation();
DROP TABLE IF EXISTS approval_band_sets;
DROP FUNCTION IF EXISTS forbid_approval_band_set_mutation();
"""
)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Loud refusal first (0017/0020/0031 precedent), documented in the
    # module docstring; a clean expansion reverses without loss.
    op.execute(_DOWN)
