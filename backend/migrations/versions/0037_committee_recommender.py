"""committee-recommender attribution: recommended_by on loan_applications

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-05

Additive revision closing the committee-recommender contract
gap (status ledger note 3646960565 item 2: the web applications module
documents the recommender as per-tab-witness-only in makerRegistry.ts),
claimed as 0037 with down_revision '0036' in its MR description and
the build plan registry at branch time (the migration-declaration rule; head 0036 + the open-MR
list verified — no other in-flight claim).

  * loan_applications.recommended_by — the acting principal that moved
    the application INTO the committee stage (the "refer to committee"
    recommendation), recorded at that transition from then on
    (application/loan_applications.py:transition_stage). Powers the
    recommender separation-of-duties checks (the recommender can neither vote on nor disburse the
    application they put before the committee — the exit-requester-cannot-vote / write-off-proposer
    / initiator-cannot-disburse posture, applied consistently) and
    rides the ApplicationOut read model as the bare staff UUID (least
    disclosure; resolving it stays behind access_control). NULL for
    system_actor transitions (an honest "moved by the system") and for
    pre-0037 rows without unambiguous history — attribution is never
    invented.

  * Backfill — ONLY from each row's own in-transaction audit record
    (the 0031/0036 truthful-history rule; audit rows are written in
    the same transaction as the transition they describe, so they are
    the one authentic source): action='application.stage' rows whose
    after->>'stage' = 'committee', joined on (tenant_id, entity,
    entity_id) via idx_audit_entity (0001), and ONLY where exactly one
    distinct non-NULL actor exists across the row's committee
    transitions (HAVING count(DISTINCT actor_id) = 1) — an ambiguous
    or actorless history stays NULL. loan_applications carries no
    append-only fence, so no trigger toggle is needed (unlike 0036's
    transactions leg).

  * NO new indexes (v1.1 rule; EXPLAIN posture documented in): no
    read path filters or orders by recommended_by — both SoD checks
    read the column under the application row lock already taken by PK
    (cast_vote and disburse_loan hold it FOR UPDATE), the read models
    only ADD the column to their SELECT lists (existing keyset indexes
    unchanged), and FK validation on INSERT/UPDATE is a users-PK
    lookup. No hot path exists to serve, so no index is shipped.

  * NO RLS changes: loan_applications already carries forced RLS
    (0001); TENANT_TABLES and the leakage suite are unchanged.

Downgrade REFUSES LOUDLY (the 0017/0020/0035/0036 precedent) when any
recorded attribution exists: silently dropping WHO referred an
application to the committee would recreate the very contract gap this
revision closes, as data loss. On a clean expansion (migrate-check's
up -> down -> up) it reverses exactly.
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE loan_applications
    ADD COLUMN recommended_by uuid REFERENCES users(id) ON DELETE RESTRICT;

-- Backfill from each application's own in-transaction audit record
-- (action='application.stage' is written atomically with the stage
-- UPDATE it describes; the committee entry is after->>'stage' =
-- 'committee'). Only unambiguous history is copied: exactly one
-- distinct non-NULL actor across the row's committee transitions,
-- else the row stays NULL (attribution is never invented).
UPDATE loan_applications la
SET recommended_by = src.actor_id
FROM (
    SELECT tenant_id, entity_id, min(actor_id::text)::uuid AS actor_id
    FROM audit_log
    WHERE entity = 'loan_applications'
      AND action = 'application.stage'
      AND after->>'stage' = 'committee'
      AND actor_id IS NOT NULL
    GROUP BY tenant_id, entity_id
    HAVING count(DISTINCT actor_id) = 1
) src
WHERE la.tenant_id = src.tenant_id
  AND la.id::text = src.entity_id
  AND la.recommended_by IS NULL;
"""

_DOWN_GUARD = """
DO $guard$
DECLARE
    attributed_applications bigint;
BEGIN
    SELECT count(*) INTO attributed_applications
        FROM loan_applications WHERE recommended_by IS NOT NULL;
    IF attributed_applications > 0 THEN
        RAISE EXCEPTION USING MESSAGE = format(
            'refusing downgrade 0037: recorded recommender attribution '
            'exists (%s attributed loan applications). Dropping WHO '
            'referred an application to the committee would recreate '
            'the issue-#30 recommender contract gap as data loss (the '
            '0017/0020/0036 refusal precedent) - attribution is never '
            'silently destroyed.',
            attributed_applications);
    END IF;
END
$guard$;
"""

_DOWN = """
ALTER TABLE loan_applications DROP COLUMN IF EXISTS recommended_by;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Loud refusal first (0017/0020/0035/0036 precedent), documented in
    # the module docstring; a clean expansion reverses without loss.
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN)
