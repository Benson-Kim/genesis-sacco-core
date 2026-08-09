"""idempotency key expiry (.17c / DSA-3)

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-02

Claimed as 0029 with down_revision 0028 (this MR's own (b) claim; the
chain 0026 <- 0027 <- 0028 <- 0029 was declared up front per v1.2 rule
14 — one number per separable item). Expand-only objects backing
(c):

  * idempotency_keys.expires_at — the replay-retention cutoff. The
    application sets it explicitly on every claim from
    Settings.idempotency_retention_hours (server config, —
    never caller-supplied; no request field exists for it and the
    middleware computes it in SQL as now() + make_interval). The
    column DEFAULT here serves two purposes and is NOT the config
    source of truth:
      - it backfills every pre-0029 row in the same statement
        (PostgreSQL fast default — no table rewrite), so the fence
        `expires_at > now()` starts expiring legacy rows 24h after
        this migration runs instead of grandfathering them forever;
      - it keeps the pre-0029 application INSERT (which names no
        expires_at) working for one release (expand -> migrate -> contract), matching the Settings
        default of 24 hours.

  * idx_idempotency_keys_expiry — the driving index for the retention
    purge's FOR UPDATE SKIP LOCKED subquery (scalability: shipped in the same MR as the query it
    serves, application/idempotency_purge.py:PURGE_BATCH_SQL). The replay
    lookup itself stays on the (tenant_id, key) UNIQUE from 0001.

NO new tables: no TENANT_TABLES / ENTITY_MODULES / RLS delta —
idempotency_keys has carried forced RLS and its leakage-suite entry
since 0001.

Insider posture (DB-level, honest): a direct-SQL UPDATE of expires_at
cannot be fenced by a CHECK (CHECK cannot reference now()). Tampering
can only SHORTEN a key's replay window — degrading an expired key to a
NEW request with its own single effect (the exactly-one-effect property, enforced by the atomic
takeover in api/idempotency.py) — or
EXTEND how long a stored non-money response envelope replays; it can
never produce a second money effect for one claim epoch.

Lock posture (0024 honesty precedent): ALTER TABLE ADD COLUMN with a
constant-foldable default takes ACCESS EXCLUSIVE on idempotency_keys
briefly (fast default, no rewrite); CREATE INDEX (non-CONCURRENT)
takes SHARE, blocking claim INSERTs — i.e. mutating requests carrying
an Idempotency-Key — for the build. Within this project's accepted
maintenance-window migration model.

Downgrade drops the index and the column with NO conditional refusal,
stated deliberately (the 0017/0020 refusal precedent does not apply):
idempotency rows are a bounded replay CACHE — claim markers plus
stored response envelopes — not money-bearing append-only history;
every money effect they guard lives in ledger_entries / audit_log,
which no path here touches. Dropping expires_at restores exactly the
pre-0029 replay-forever semantics.
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_UP = """
-- Replay-retention cutoff (DSA-3). The DEFAULT backfills pre-0029
-- rows and is the compatibility floor for one release; the
-- application supplies the value explicitly from server config on
-- every claim (api/idempotency.py — never caller-supplied).
ALTER TABLE idempotency_keys
    ADD COLUMN expires_at timestamptz NOT NULL
    DEFAULT (now() + interval '24 hours');

-- Driving index for the purge's FOR UPDATE SKIP LOCKED subquery
-- (application/idempotency_purge.py:PURGE_BATCH_SQL): oldest expired
-- rows per tenant, bounded batches (gate 1.3 — ships with its query).
CREATE INDEX idx_idempotency_keys_expiry
    ON idempotency_keys (tenant_id, expires_at);
"""

_DOWN = """
-- No conditional refusal, deliberately (see module docstring):
-- idempotency rows are a replay cache, not money-bearing history —
-- the effects they guard live in ledger_entries/audit_log, untouched
-- here. Dropping the column restores the pre-0029 replay-forever
-- semantics exactly.
DROP INDEX IF EXISTS idx_idempotency_keys_expiry;
ALTER TABLE idempotency_keys DROP COLUMN expires_at;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
