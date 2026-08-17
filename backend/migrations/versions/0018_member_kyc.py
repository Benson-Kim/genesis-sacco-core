"""member KYC profiles & documents (P13.12)

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-01

EXPAND-ONLY revision backing BUILD_PROMPTS P13.12 (GAP_ANALYSIS §2.3):
the prototype's type-specific registration data and per-type document
checklist, previously not persisted at all.

  1. members UNIQUE (id, type) — a redundant-with-PK unique key whose
     only purpose is to let the new tables carry a composite FK
     (member_id, member_type) -> members (id, type): the copied
     member_type column that the per-type CHECKs below validate against
     can never drift from the member row it belongs to (gate 1.5 —
     consistency in the DATABASE, not just the app).

     The FK deliberately carries no ON UPDATE action (review K3):
     members.type is immutable in the P8 service — no application path
     updates it (update_member writes name/phone/email, the status
     paths write status, and no request body accepts a type field), so
     the raw-FK-violation failure mode is unreachable through the app.
     The invariant is pinned by
     test_members_type_immutable_no_app_path_and_db_backstop, and for
     manual SQL the violation is the intended backstop: a type rewrite
     under an existing profile/document row is refused by the database
     instead of silently detaching the per-type CHECKs.

  2. member_profiles — one row per member (UNIQUE (tenant_id,
     member_id) is the atomic-claim key, v1.1 rule 5) holding:
       * member_type (composite-FK-synced, see 1) with a CHECK that the
         per-type JSONB profile carries EXACTLY the prototype's
         sections (Person bio/contact/employment/next-of-kin; Company
         registration/office/contact/signatories; Group registration/
         officials; Vehicle vehicle/compliance/ownership) — the `?&`
         containment plus key-subtraction-to-empty enforces both
         presence and absence of top-level sections. Deep field
         validation is domain-owned (domain/member_kyc.py); this CHECK
         is the raw-SQL backstop.
       * category — the prototype "Member category" field ("Ordinary");
         free vocabulary owned by clients (the P12 exit-reason
         precedent), blank refused.
       * dpa_consent_at — the DPA-2019 consent flag captured at
         registration, timestamped, and IMMUTABLE once set: the
         consent-guard trigger below refuses ANY change to a non-null
         value and refuses DELETE of a row carrying consent, so not
         even raw SQL through the app role can rewrite consent
         evidence (P13.12 requirement: DB-enforced, not just
         application-enforced).
       * version for optimistic locking (gate 1.4).

  3. member_documents — the per-type document checklist as metadata
     rows (type, status, expiry). Binary content is deliberately NOT
     stored: upload is deferred behind a storage-adapter decision
     (ADR-0003); metadata-only is the recorded P13.12 fallback.
       * doc_type is CHECKed per member_type against the code-owned
         checklists (prototype docsFor): a logbook can never attach to
         a person, a passport photo never to a vehicle.
       * status walks pending -> received -> verified/rejected (and
         rejected -> received on resubmission) — the transition map is
         domain-owned; the CHECK pins the value set.
       * UNIQUE (tenant_id, member_id, doc_type) is both the atomic
         checklist claim (v1.1 rule 5) AND the keyset-listing index
         (gate 1.3): the documents page orders by doc_type within one
         member, so the constraint's index serves every page
         (EXPLAIN-asserted in tests/test_p1312_explain.py).

  4. RLS enabled + FORCED on both tables with the 0001 tenant_isolation
     policy shape (ADR-0002); the leakage suite is extended to them.

Downgrade fully reverses this revision: it drops only the new objects
(profiles/documents created after upgrade are dropped with their
tables) and touches no pre-existing data.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_UP = """
-- -------------------------------------------------------------------------
-- 1. Composite-FK anchor: members (id, type)
-- -------------------------------------------------------------------------
ALTER TABLE members ADD CONSTRAINT uq_members_id_type UNIQUE (id, type);

-- -------------------------------------------------------------------------
-- 2. member_profiles (P13.12)
-- -------------------------------------------------------------------------
CREATE TABLE member_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL,
    member_type text NOT NULL
        CHECK (member_type IN ('person', 'company', 'group', 'vehicle')),
    category text NOT NULL CHECK (btrim(category) <> ''),
    profile jsonb NOT NULL,
    dpa_consent_at timestamptz,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_member_profiles_member UNIQUE (tenant_id, member_id),
    CONSTRAINT fk_member_profiles_member_type
        FOREIGN KEY (member_id, member_type)
        REFERENCES members (id, type) ON DELETE RESTRICT,
    -- Per-type section shape: required sections present (?&) and no
    -- foreign sections left after subtracting them (backstop for raw
    -- SQL; deep field validation is domain-owned).
    CONSTRAINT ck_member_profiles_shape CHECK (
        jsonb_typeof(profile) = 'object'
        AND (
            (member_type = 'person'
             AND profile ?& ARRAY['bio', 'contact', 'employment', 'next_of_kin']
             AND profile - 'bio' - 'contact' - 'employment' - 'next_of_kin'
                 = '{}'::jsonb)
            OR (member_type = 'company'
             AND profile ?& ARRAY['registration', 'office', 'contact', 'signatories']
             AND profile - 'registration' - 'office' - 'contact' - 'signatories'
                 = '{}'::jsonb)
            OR (member_type = 'group'
             AND profile ?& ARRAY['registration', 'officials']
             AND profile - 'registration' - 'officials' = '{}'::jsonb)
            OR (member_type = 'vehicle'
             AND profile ?& ARRAY['vehicle', 'compliance', 'ownership']
             AND profile - 'vehicle' - 'compliance' - 'ownership' = '{}'::jsonb)
        )
    )
);

-- DPA-2019 consent immutability (DB-enforced): once captured, the
-- timestamp can never change, and the row carrying it can never be
-- deleted — consent evidence survives raw SQL through the app role.
CREATE FUNCTION member_profiles_consent_guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'member profile with captured DPA consent cannot be deleted (P13.12)';
    END IF;
    IF OLD.dpa_consent_at IS NOT NULL
       AND NEW.dpa_consent_at IS DISTINCT FROM OLD.dpa_consent_at THEN
        RAISE EXCEPTION
            'member_profiles.dpa_consent_at is immutable once set (P13.12)';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TRIGGER member_profiles_consent_update_guard
    BEFORE UPDATE ON member_profiles
    FOR EACH ROW EXECUTE FUNCTION member_profiles_consent_guard();
CREATE TRIGGER member_profiles_consent_delete_guard
    BEFORE DELETE ON member_profiles
    FOR EACH ROW WHEN (OLD.dpa_consent_at IS NOT NULL)
    EXECUTE FUNCTION member_profiles_consent_guard();

-- -------------------------------------------------------------------------
-- 3. member_documents (P13.12) — metadata only; binary deferred (ADR-0003)
-- -------------------------------------------------------------------------
CREATE TABLE member_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    member_id uuid NOT NULL,
    member_type text NOT NULL
        CHECK (member_type IN ('person', 'company', 'group', 'vehicle')),
    doc_type text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'received', 'verified', 'rejected')),
    expires_at date,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_member_documents_checklist
        UNIQUE (tenant_id, member_id, doc_type),
    CONSTRAINT fk_member_documents_member_type
        FOREIGN KEY (member_id, member_type)
        REFERENCES members (id, type) ON DELETE RESTRICT,
    -- Per-type checklist (prototype docsFor), mirrored from the
    -- code-owned map in domain/member_kyc.py.
    CONSTRAINT ck_member_documents_type_matches CHECK (
        (member_type = 'person' AND doc_type IN
            ('national_id_copy', 'kra_pin', 'passport_photo', 'signature',
             'next_of_kin_id'))
        OR (member_type = 'company' AND doc_type IN
            ('cert_of_incorporation', 'cr12', 'kra_pin', 'board_resolution',
             'signatory_ids'))
        OR (member_type = 'group' AND doc_type IN
            ('registration_cert', 'constitution', 'members_list', 'resolution',
             'officials_ids'))
        OR (member_type = 'vehicle' AND doc_type IN
            ('logbook', 'insurance_cert', 'ntsa_inspection', 'psv_tlb_licence',
             'sacco_agreement'))
    )
);

-- -------------------------------------------------------------------------
-- 4. RLS: enable + force + tenant_isolation policy, matching 0001
-- -------------------------------------------------------------------------
ALTER TABLE member_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE member_profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON member_profiles
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

ALTER TABLE member_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE member_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON member_documents
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
"""

_DOWN = """
DROP TABLE IF EXISTS member_documents;

DROP TRIGGER IF EXISTS member_profiles_consent_delete_guard ON member_profiles;
DROP TRIGGER IF EXISTS member_profiles_consent_update_guard ON member_profiles;
DROP TABLE IF EXISTS member_profiles;
DROP FUNCTION IF EXISTS member_profiles_consent_guard();

ALTER TABLE members DROP CONSTRAINT IF EXISTS uq_members_id_type;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
