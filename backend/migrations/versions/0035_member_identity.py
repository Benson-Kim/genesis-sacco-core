"""member identity: credential link table + principal columns

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-04

Additive revision backing the build plan (member identity &
member-facing auth), claimed as 0035 with down_revision '0034' in the
MR description at branch time (the migration-declaration rule; head 0034 re-verified against
main at branch time).

  * member_credentials — the explicit, audited member <-> credential
    link that RETIRES the interim users.email <-> members.email
    identity bridge. One row per issued credential;
    revocations are recorded, never deleted (identity history stays
    forensic like audit_log).
    - THE LINK IS THE AUTHORITY: member authentication and
      every member-principal authorization resolve through this table;
      rewriting users.email or members.email redirects nothing.
    - Atomic claims: partial UNIQUE
      uq_member_credentials_email_active (tenant_id, email) WHERE
      status = 'active' is claimed with INSERT... ON CONFLICT DO
      NOTHING checked by rowcount — a concurrent double-link of the
      same email lands exactly one row. One active credential per
      member is enforced by uq_member_credentials_member_active,
      checked under the member row lock (link mutations lock the member row — chain ROOT,
      lock-order.md).
    - (link takeover): there is NO self-service mutation path;
      creating/revoking a link is an audited admin mutation
      (member_identity:* permissions). Moving an email to another
      member requires revoke + create — two audited mutations.
    - The email-active UNIQUE doubles as the member-login serving
      index (the 0019/0026 claim-key-as-index precedent); EXPLAIN in
      tests/test_p145_explain.py -> backend/perf/explain_p145.txt.

  * otp_challenges / refresh_tokens — the P3 machinery EXPANDED to a
    second principal, not forked (reuse-first): nullable
    member_credential_id, user_id relaxed to nullable, and an
    exactly-one-principal XOR CHECK. Existing staff rows satisfy the
    CHECK unchanged (user set, credential NULL).

  * guarantees — consent becomes an act of a principal:
    - consented_by_credential_id: the member principal that consented
      (the P9 consent contract, now first-class);
    - consent_attested_by + consent_reference: the explicit
      STAFF-ATTESTED OVERRIDE (its own audit category guarantee.consent_override and its own
      permission member_identity:approve — the substitution-consent lesson);
      ck_guarantees_attested_consent_reference makes an attestation
      without cited evidence unrepresentable.
    - guarantee_consent_requires_principal (constraint trigger, the
      0028/0031/0034 DDL-guard precedent): any row ENTERING status
      'active' (INSERT or UPDATE) without a member principal OR a
      staff attestation is refused AT THE DATABASE — a consent written
      without a principal fails the DB constraint (falsifiable by dropping the trigger). Rows
      already active (earlier history)
      are untouched: the trigger guards transitions, never rewrites.

  * RLS enabled AND FORCED with the 0001 tenant_isolation policy shape
    (ADR-0002); member_credentials joins TENANT_TABLES and the leakage
    suite in this MR.

Downgrade REFUSES LOUDLY (the 0017/0020 precedent) when identity- or
consent-bearing data exists: member credentials, member-principal OTP
challenges or refresh families, or principal-carrying consents cannot
be silently destroyed — identity is the authorization substrate for
member-visible money actions. On an empty expansion (migrate-check's
up -> down -> up) it reverses cleanly.
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE member_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    email text NOT NULL CHECK (length(email) BETWEEN 3 AND 254),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    -- A revoked credential always records when; an active one never
    -- does (the 0026 ck_recovery_cases_closed_at shape).
    CONSTRAINT ck_member_credentials_revoked_at
        CHECK ((status = 'active') = (revoked_at IS NULL))
);

-- The link claim (v1.1 rule 5) AND the member-login serving index:
-- one ACTIVE credential per email, claimed atomically with
-- INSERT ... ON CONFLICT DO NOTHING + rowcount.
CREATE UNIQUE INDEX uq_member_credentials_email_active
    ON member_credentials (tenant_id, email) WHERE status = 'active';

-- One ACTIVE credential per member; checked under the member row lock
-- (link mutations serialise there), backstopped here at the DB.
CREATE UNIQUE INDEX uq_member_credentials_member_active
    ON member_credentials (tenant_id, member_id) WHERE status = 'active';

-- FK index beyond the partial unique: revoked history rows are
-- outside it (gate 1.3/1.5; the 0026 idx_recovery_cases_loan shape).
CREATE INDEX idx_member_credentials_member
    ON member_credentials (tenant_id, member_id);

ALTER TABLE member_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE member_credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON member_credentials
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- P3 machinery expanded to the member principal (never forked).
ALTER TABLE otp_challenges
    ADD COLUMN member_credential_id uuid
        REFERENCES member_credentials(id) ON DELETE CASCADE;
ALTER TABLE otp_challenges ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE otp_challenges ADD CONSTRAINT ck_otp_challenges_one_principal
    CHECK ((user_id IS NULL) <> (member_credential_id IS NULL));
CREATE INDEX idx_otp_credential
    ON otp_challenges (tenant_id, member_credential_id, created_at);

ALTER TABLE refresh_tokens
    ADD COLUMN member_credential_id uuid
        REFERENCES member_credentials(id) ON DELETE CASCADE;
ALTER TABLE refresh_tokens ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_one_principal
    CHECK ((user_id IS NULL) <> (member_credential_id IS NULL));
CREATE INDEX idx_refresh_credential
    ON refresh_tokens (tenant_id, member_credential_id);

-- Consent carries its principal (FM4).
ALTER TABLE guarantees
    ADD COLUMN consented_by_credential_id uuid
        REFERENCES member_credentials(id) ON DELETE RESTRICT,
    ADD COLUMN consent_attested_by uuid
        REFERENCES users(id) ON DELETE RESTRICT,
    ADD COLUMN consent_reference text
        CHECK (consent_reference IS NULL
               OR length(consent_reference) BETWEEN 1 AND 500);
ALTER TABLE guarantees ADD CONSTRAINT ck_guarantees_attested_reference
    CHECK (consent_attested_by IS NULL OR consent_reference IS NOT NULL);
CREATE INDEX idx_guarantees_consent_credential
    ON guarantees (tenant_id, consented_by_credential_id);
CREATE INDEX idx_guarantees_consent_attestor
    ON guarantees (tenant_id, consent_attested_by);

-- FM4 backstop: a guarantee may ENTER 'active' only carrying either
-- the consenting member principal or an explicit staff attestation.
-- Guards transitions only (INSERT as active, or UPDATE into active):
-- pre-P14.5 active rows are untouched and release/substitution
-- transitions OUT of active never fire it. No locking probe needed
-- (the 0030/0031 posture): the check reads only NEW/OLD of its own
-- row — no cross-row sum, so MVCC suffices.
CREATE FUNCTION guarantee_consent_requires_principal() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.status = 'active'
       AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'active')
       AND NEW.consented_by_credential_id IS NULL
       AND NEW.consent_attested_by IS NULL THEN
        RAISE EXCEPTION USING
            MESSAGE = 'guarantee consent requires a member principal or '
                      'a staff attestation (P14.5 FM4)',
            ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER guarantees_consent_principal
    BEFORE INSERT OR UPDATE ON guarantees
    FOR EACH ROW EXECUTE FUNCTION guarantee_consent_requires_principal();
"""

_DOWN_GUARD = """
DO $guard$
DECLARE
    credential_rows bigint;
    member_otp_rows bigint;
    member_refresh_rows bigint;
    principal_consents bigint;
BEGIN
    SELECT count(*) INTO credential_rows FROM member_credentials;
    SELECT count(*) INTO member_otp_rows
        FROM otp_challenges WHERE member_credential_id IS NOT NULL;
    SELECT count(*) INTO member_refresh_rows
        FROM refresh_tokens WHERE member_credential_id IS NOT NULL;
    SELECT count(*) INTO principal_consents
        FROM guarantees
        WHERE consented_by_credential_id IS NOT NULL
           OR consent_attested_by IS NOT NULL;
    IF credential_rows > 0 OR member_otp_rows > 0
       OR member_refresh_rows > 0 OR principal_consents > 0 THEN
        RAISE EXCEPTION USING MESSAGE = format(
            'refusing downgrade 0035: identity/consent-bearing data exists '
            '(%s member credentials, %s member OTP challenges, '
            '%s member refresh tokens, %s principal-carrying consents). '
            'Member identity is the authorization substrate for member '
            'actions and consent provenance is money-relevant history '
            '(the 0017/0020 refusal precedent) - it is never silently '
            'destroyed.',
            credential_rows, member_otp_rows,
            member_refresh_rows, principal_consents);
    END IF;
END
$guard$;
"""

_DOWN = """
DROP TRIGGER IF EXISTS guarantees_consent_principal ON guarantees;
DROP FUNCTION IF EXISTS guarantee_consent_requires_principal();
ALTER TABLE guarantees DROP CONSTRAINT IF EXISTS ck_guarantees_attested_reference;
ALTER TABLE guarantees
    DROP COLUMN IF EXISTS consented_by_credential_id,
    DROP COLUMN IF EXISTS consent_attested_by,
    DROP COLUMN IF EXISTS consent_reference;
ALTER TABLE refresh_tokens DROP CONSTRAINT IF EXISTS ck_refresh_tokens_one_principal;
ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS member_credential_id;
ALTER TABLE refresh_tokens ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE otp_challenges DROP CONSTRAINT IF EXISTS ck_otp_challenges_one_principal;
ALTER TABLE otp_challenges DROP COLUMN IF EXISTS member_credential_id;
ALTER TABLE otp_challenges ALTER COLUMN user_id SET NOT NULL;
DROP TABLE IF EXISTS member_credentials;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    # Loud refusal first (0017/0020 precedent), documented in the
    # module docstring; a clean expansion reverses without loss.
    op.execute(_DOWN_GUARD)
    op.execute(_DOWN)
