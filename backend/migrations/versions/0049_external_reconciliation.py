"""external reconciliation: statement staging, matching, break management

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-19

Expand-only revision backing issue #9 (INSTITUTIONAL_GAP_REGISTER G3):
no process compared MP-/BK- postings against actual M-Pesa or bank
statements — the control that catches "loans cleared without real
payment". The 0043 transactions.external_ref column is the matching
key this revision was always waiting for.

  * recon_statements — one ingested external statement batch per
    (channel, business date). The duplicate-upload claim is the
    UNIQUE (tenant_id, channel, statement_date, checksum): re-keying
    the same statement is a 409 at the atomic INSERT (v1.1 rule 5),
    while a CORRECTED statement (different content, different
    checksum) ingests as a fresh batch. Status machine
    ingested -> matched -> signed_off is enforced by the application's
    single transition path under FOR UPDATE; sign-off is four-eyes
    (a different, non-assurance principal than the ingester — the
    application/sod.py guard).

  * recon_statement_lines — the append-shaped staging rows (INSERTed
    once at ingest; the ONLY post-insert mutation is the one-shot
    match_status/matched_transaction_id fill by the matching run).
    UNIQUE (tenant_id, statement_id, external_ref): a statement that
    lists the same external reference twice is refused at ingest —
    upstream rails do not reuse confirmation codes within a batch.
    The CHECK ties matched/amount_mismatch to a recorded transaction
    and pending/statement_only to NULL — a claimed match without its
    transaction is unrepresentable.

  * recon_breaks — the work queue: ledger_only (posting with no
    statement evidence — the fraud catch), statement_only (external
    money the ledger never saw), amount_mismatch. Resolution NEVER
    touches money here: it records the reference of the correction
    posted through the EXISTING correction paths (reversing entries
    only — the ledger doctrine); the CHECKs make a resolved break
    without resolver/timestamp/reference/note unrepresentable. The
    partial UNIQUE on (tenant_id, transaction_id) WHERE kind =
    'ledger_only' prevents duplicate ledger-only breaks when two
    same-day statements sweep the same posting.

  * idx_txns_channel_time — (tenant_id, channel, occurred_at): the
    ledger-only sweep's range probe (gate 1.3: the index ships with
    the query that needs it; the existing idx_transactions_time has no
    channel leg).

  * RLS enabled AND FORCED on all three tables with the 0001
    tenant_isolation policy shape (ADR-0002); they join TENANT_TABLES
    and the leakage suite in this MR.

Downgrade REFUSES LOUDLY (the 0017/0020/0030 conditional-refusal
precedent) when any recon_statements row exists: reconciliation
history is regulatory control evidence and is never silently
destroyed. On an empty expansion (migrate-check's up -> down -> up) it
reverses cleanly.
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_UP = """
-- ---------------------------------------------------------------------------
-- 1. Statement batches (the ingest claim)
-- ---------------------------------------------------------------------------
CREATE TABLE recon_statements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    channel text NOT NULL CHECK (channel IN ('mpesa', 'bank')),
    statement_date date NOT NULL,
    source text NOT NULL
        CHECK (btrim(source) <> '' AND char_length(source) <= 120),
    -- sha256 hex of the canonical line set: the duplicate-upload claim key.
    checksum text NOT NULL CHECK (char_length(checksum) = 64),
    line_count integer NOT NULL CHECK (line_count > 0),
    status text NOT NULL DEFAULT 'ingested'
        CHECK (status IN ('ingested', 'matched', 'signed_off')),
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    signed_off_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    signed_off_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- A signed-off batch always records who and when; others never do.
    CONSTRAINT ck_recon_statements_signoff_at
        CHECK ((status = 'signed_off') = (signed_off_at IS NOT NULL)),
    CONSTRAINT ck_recon_statements_signoff_by
        CHECK ((status = 'signed_off') = (signed_off_by IS NOT NULL)),
    CONSTRAINT uq_recon_statements_batch
        UNIQUE (tenant_id, channel, statement_date, checksum)
);
CREATE INDEX idx_recon_statements_day
    ON recon_statements (tenant_id, channel, statement_date);
CREATE INDEX idx_recon_statements_created_by
    ON recon_statements (tenant_id, created_by);
CREATE INDEX idx_recon_statements_signed_off_by
    ON recon_statements (tenant_id, signed_off_by);

-- ---------------------------------------------------------------------------
-- 2. Statement lines (staging; one-shot match fill)
-- ---------------------------------------------------------------------------
CREATE TABLE recon_statement_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    statement_id uuid NOT NULL REFERENCES recon_statements(id) ON DELETE RESTRICT,
    line_no integer NOT NULL CHECK (line_no >= 1),
    -- Mirrors the 0043 transactions.external_ref bounds exactly.
    external_ref text NOT NULL CHECK (char_length(external_ref) BETWEEN 2 AND 40),
    -- Signed statement amount (credit positive / debit negative);
    -- matching compares magnitudes against the always-positive
    -- transactions.amount.
    amount numeric(18,2) NOT NULL CHECK (amount <> 0),
    occurred_on date NOT NULL,
    narrative text CHECK (narrative IS NULL OR char_length(narrative) <= 200),
    match_status text NOT NULL DEFAULT 'pending'
        CHECK (match_status IN ('pending', 'matched', 'amount_mismatch', 'statement_only')),
    matched_transaction_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    -- A (mis)match always records its transaction; pending /
    -- statement_only never do.
    CONSTRAINT ck_recon_lines_match_txn
        CHECK ((match_status IN ('matched', 'amount_mismatch'))
               = (matched_transaction_id IS NOT NULL)),
    CONSTRAINT uq_recon_lines_position UNIQUE (tenant_id, statement_id, line_no),
    CONSTRAINT uq_recon_lines_ref UNIQUE (tenant_id, statement_id, external_ref)
);
-- The "already matched by ANY statement" anti-join probe of the
-- ledger-only sweep (gate 1.3).
CREATE INDEX idx_recon_lines_matched_txn
    ON recon_statement_lines (tenant_id, matched_transaction_id)
    WHERE matched_transaction_id IS NOT NULL;
CREATE INDEX idx_recon_lines_statement_status
    ON recon_statement_lines (tenant_id, statement_id, match_status);

-- ---------------------------------------------------------------------------
-- 3. Breaks (the aging work queue)
-- ---------------------------------------------------------------------------
CREATE TABLE recon_breaks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    statement_id uuid NOT NULL REFERENCES recon_statements(id) ON DELETE RESTRICT,
    kind text NOT NULL
        CHECK (kind IN ('ledger_only', 'statement_only', 'amount_mismatch')),
    statement_line_id uuid REFERENCES recon_statement_lines(id) ON DELETE RESTRICT,
    transaction_id uuid REFERENCES transactions(id) ON DELETE RESTRICT,
    external_ref text
        CHECK (external_ref IS NULL OR char_length(external_ref) BETWEEN 2 AND 40),
    ledger_amount numeric(18,2),
    statement_amount numeric(18,2),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolved_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    resolved_at timestamptz,
    -- The reference of the correction posted through the EXISTING
    -- correction paths (e.g. an RV-/FE- txn_ref) or the operator's
    -- external evidence citation — never a money mutation here.
    resolution_reference text
        CHECK (resolution_reference IS NULL OR char_length(resolution_reference) <= 60),
    resolution_note text
        CHECK (resolution_note IS NULL OR char_length(resolution_note) <= 500),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- Kind shape: ledger_only carries the transaction and no line;
    -- statement_only the line and no transaction; amount_mismatch both.
    CONSTRAINT ck_recon_breaks_kind_shape CHECK (
        (kind = 'ledger_only'
         AND transaction_id IS NOT NULL AND statement_line_id IS NULL)
        OR (kind = 'statement_only'
            AND statement_line_id IS NOT NULL AND transaction_id IS NULL)
        OR (kind = 'amount_mismatch'
            AND statement_line_id IS NOT NULL AND transaction_id IS NOT NULL)
    ),
    -- A resolved break always records who/when/what evidence; an open
    -- one never does (the 0026 closed_at shape, all four legs).
    CONSTRAINT ck_recon_breaks_resolved_at
        CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CONSTRAINT ck_recon_breaks_resolved_by
        CHECK ((status = 'resolved') = (resolved_by IS NOT NULL)),
    CONSTRAINT ck_recon_breaks_resolution_reference
        CHECK ((status = 'resolved') = (resolution_reference IS NOT NULL)),
    CONSTRAINT ck_recon_breaks_resolution_note
        CHECK ((status = 'resolved') = (resolution_note IS NOT NULL))
);
-- One ledger-only break per posting, ever (two same-day statements
-- sweeping the same transaction collapse to one work item).
CREATE UNIQUE INDEX uq_recon_breaks_ledger_only_txn
    ON recon_breaks (tenant_id, transaction_id)
    WHERE kind = 'ledger_only';
-- One break per statement line (the line's classification is one-shot).
CREATE UNIQUE INDEX uq_recon_breaks_line
    ON recon_breaks (tenant_id, statement_line_id)
    WHERE statement_line_id IS NOT NULL;
-- The aging queue: open-first, oldest-first keyset (gate 1.3).
CREATE INDEX idx_recon_breaks_queue
    ON recon_breaks (tenant_id, status, created_at, id);
CREATE INDEX idx_recon_breaks_statement
    ON recon_breaks (tenant_id, statement_id);
CREATE INDEX idx_recon_breaks_resolved_by
    ON recon_breaks (tenant_id, resolved_by);

-- ---------------------------------------------------------------------------
-- 4. The ledger-only sweep's range probe (gate 1.3)
-- ---------------------------------------------------------------------------
CREATE INDEX idx_txns_channel_time
    ON transactions (tenant_id, channel, occurred_at);

-- ---------------------------------------------------------------------------
-- 5. RLS: enable + force + tenant_isolation (ADR-0002, the 0001 shape)
-- ---------------------------------------------------------------------------
ALTER TABLE recon_statements ENABLE ROW LEVEL SECURITY;
ALTER TABLE recon_statements FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recon_statements
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE recon_statement_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE recon_statement_lines FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recon_statement_lines
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE recon_breaks ENABLE ROW LEVEL SECURITY;
ALTER TABLE recon_breaks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recon_breaks
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN_GUARD = """
DO $$
DECLARE
    batches bigint;
BEGIN
    SELECT count(*) INTO batches FROM recon_statements;
    IF batches > 0 THEN
        RAISE EXCEPTION
            'refusing downgrade: % reconciliation batch(es) exist — '
            'recon history is regulatory control evidence (issue #9); '
            'export and archive it before downgrading', batches;
    END IF;
END
$$;
"""

_DOWN = """
DROP INDEX idx_txns_channel_time;
DROP TABLE recon_breaks;
DROP TABLE recon_statement_lines;
DROP TABLE recon_statements;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN)
