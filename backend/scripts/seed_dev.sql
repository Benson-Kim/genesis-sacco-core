-- =============================================================================
-- Genesis Prestige — development seed data
-- =============================================================================
-- Run from the backend/ directory THROUGH THE GUARDED RUNNER (#48):
--   ALLOW_DESTRUCTIVE_SEED=1 python scripts/seed_dev.py
--
-- Do NOT invoke this file with raw psql: the CLEANUP section TRUNCATEs
-- every tenant table (CASCADE), and TRUNCATE bypasses the append-only
-- trigger on ledger_entries. The runner enforces the fail-closed dev-DSN
-- guard (#34/#48) BEFORE any connection is opened. The DO block below is
-- a weaker second layer for direct psql invocation that bypasses the
-- runner — it fires only after a connection already exists.
--
-- RLS note: every tenant-scoped table has FORCE ROW LEVEL SECURITY so the
-- very first INSERT (the tenant row itself) must come from a superuser or a
-- BYPASSRLS role. The `genesis` user from docker-compose.yml is a superuser,
-- so this script "just works" against that setup.
--
-- UUID strategy: gen_random_uuid() throughout. Fixed seed UUIDs are used
-- only for the ONE row that web/.env.local references:
--   NEXT_PUBLIC_TENANT_ID = 11111111-1111-1111-1111-111111111111
-- Everything else is auto-generated so repeated seeding never collides.
-- =============================================================================

-- Suppress notices from DO blocks
SET client_min_messages TO WARNING;

BEGIN;

-- ---------------------------------------------------------------------------
-- CLEANUP: wipe all seed data so the script is safe to re-run.
-- TRUNCATE CASCADE bypasses the append-only trigger on ledger_entries
-- (that trigger guards UPDATE/DELETE, not TRUNCATE).
-- ---------------------------------------------------------------------------
TRUNCATE TABLE
    accounting_periods, dividend_distributions, dividend_declaration_votes,
    dividend_declarations, recovery_case_notes, recovery_cases,
    exit_votes, member_exits, loan_schedules, repayments, guarantees,
    ledger_entries, loans, loan_applications, transactions,
    deposit_accounts, share_accounts, member_documents, member_profiles,
    member_credentials, members, outbox_events, idempotency_keys,
    otp_challenges, refresh_tokens, permissions, users,
    loan_products, branches, tenant_settings, roles
CASCADE;
-- Keep the tenant row itself (it is referenced by everything above).
-- Re-insert handled idempotently below via ON CONFLICT DO NOTHING.

-- ---------------------------------------------------------------------------
-- 0. Enable pgcrypto (needed for gen_random_uuid if not already present)
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. Tenant (fixed UUID – matches web/.env.local NEXT_PUBLIC_TENANT_ID)
-- ---------------------------------------------------------------------------
INSERT INTO tenants (id, name, slug, status)
VALUES ('11111111-1111-1111-1111-111111111111', 'Zuri Genesis SACCO', 'zuri-genesis', 'active')
ON CONFLICT (id) DO NOTHING;

-- Convenience: remember tenant id in a local variable for the rest of the script
DO $vars$
BEGIN
    PERFORM set_config('seed.tenant_id', '11111111-1111-1111-1111-111111111111', true);
END
$vars$;

-- ---------------------------------------------------------------------------
-- 2. Branches
-- ---------------------------------------------------------------------------
INSERT INTO branches (id, tenant_id, name)
VALUES
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'HQ'),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Nairobi CBD'),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Thika'),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Mombasa'),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Kisumu')
ON CONFLICT (tenant_id, name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Tenant settings (interest, parameters, approval matrix)
-- ---------------------------------------------------------------------------
INSERT INTO tenant_settings (
    tenant_id,
    deposit_interest_annual_rate_pct,
    dividend_rate_pct,
    deposit_rebate_rate_pct,
    penalty_rate_pct_per_month,
    penalty_grace_days,
    penalty_charged_on,
    loan_interest_method,
    loan_interest_basis,
    min_share_capital,
    registration_fee,
    min_monthly_contribution,
    max_member_exposure,
    dormancy_period_months,
    financial_year_end_month,
    exit_notice_period_days,
    exit_fee,
    committee_size,
    committee_quorum
)
VALUES (
    '11111111-1111-1111-1111-111111111111',
    8.00,       -- 8% p.a. deposit interest
    10.00,      -- 10% dividend on shares
    3.00,       -- 3% deposit rebate
    1.50,       -- 1.5% penalty per month
    7,          -- 7-day grace period
    'instalment_in_arrears',
    'reducing_balance',
    'actual_365',
    5000.00,    -- min share capital KES 5,000
    500.00,     -- registration fee KES 500
    1000.00,    -- min monthly contribution KES 1,000
    3000000.00, -- max member exposure KES 3M
    6,          -- dormancy after 6 months
    12,         -- financial year ends December
    30,         -- 30-day exit notice
    1000.00,    -- exit fee KES 1,000
    3,          -- committee size
    2           -- quorum (matches the 2 approve-capable users in the seed)
)
ON CONFLICT (tenant_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. Roles
-- ---------------------------------------------------------------------------
INSERT INTO roles (id, tenant_id, name, is_system)
VALUES
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'System Admin',     true),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Branch Manager',   false),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Loan Officer',     false),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Teller',           false),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Credit Committee', false),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Accountant',       false),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Auditor',          false)
ON CONFLICT (tenant_id, name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. Permissions — only modules that exist in backend/domain/rbac.py Module enum:
--    members, applications, loan_book, transactions, reports, settings,
--    access_control, corrections, member_identity
-- ---------------------------------------------------------------------------
DO $perms$
DECLARE
    tid  uuid := '11111111-1111-1111-1111-111111111111';
    -- Exact set of values in genesis.domain.rbac.Module (rbac.py).
    -- Do NOT add names not in that enum — /me/permissions will crash.
    all_mods text[] := ARRAY[
        'members', 'applications', 'loan_book', 'transactions',
        'reports', 'settings', 'access_control', 'corrections', 'member_identity'
    ];
    mod text;
BEGIN
    -- System Admin: full access everywhere
    FOREACH mod IN ARRAY all_mods LOOP
        INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
        SELECT tid, id, mod, true, true, true, true
        FROM roles WHERE tenant_id = tid AND name = 'System Admin'
        ON CONFLICT (tenant_id, role_id, module) DO NOTHING;
    END LOOP;

    -- Branch Manager: full ops, no settings / access_control
    FOREACH mod IN ARRAY ARRAY['members','applications','loan_book','transactions','reports','corrections'] LOOP
        INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
        SELECT tid, id, mod, true, true, true, false
        FROM roles WHERE tenant_id = tid AND name = 'Branch Manager'
        ON CONFLICT (tenant_id, role_id, module) DO NOTHING;
    END LOOP;

    -- Loan Officer: applications + limited view
    INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
    SELECT tid, r.id, m.module, m.v, m.c, m.e, m.a
    FROM roles r,
    (VALUES
        ('members',      true,  false, false, false),
        ('applications', true,  true,  true,  false),
        ('loan_book',    true,  false, false, false),
        ('reports',      true,  false, false, false)
    ) AS m(module, v, c, e, a)
    WHERE r.tenant_id = tid AND r.name = 'Loan Officer'
    ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

    -- Teller: deposits/withdrawals only
    INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
    SELECT tid, r.id, m.module, m.v, m.c, m.e, m.a
    FROM roles r,
    (VALUES
        ('members',      true,  false, false, false),
        ('transactions', true,  true,  false, false)
    ) AS m(module, v, c, e, a)
    WHERE r.tenant_id = tid AND r.name = 'Teller'
    ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

    -- Credit Committee: approve applications and loan_book
    INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
    SELECT tid, r.id, m.module, m.v, m.c, m.e, m.a
    FROM roles r,
    (VALUES
        ('members',      true,  false, false, false),
        ('applications', true,  false, false, true),
        ('loan_book',    true,  false, false, true),
        ('reports',      true,  false, false, false)
    ) AS m(module, v, c, e, a)
    WHERE r.tenant_id = tid AND r.name = 'Credit Committee'
    ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

    -- Accountant: full transactions, reports, corrections; view elsewhere
    INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
    SELECT tid, r.id, m.module, m.v, m.c, m.e, m.a
    FROM roles r,
    (VALUES
        ('members',      true,  false, false, false),
        ('loan_book',    true,  false, false, false),
        ('transactions', true,  true,  true,  false),
        ('corrections',  true,  true,  true,  false),
        ('reports',      true,  true,  true,  false)
    ) AS m(module, v, c, e, a)
    WHERE r.tenant_id = tid AND r.name = 'Accountant'
    ON CONFLICT (tenant_id, role_id, module) DO NOTHING;

    -- Auditor: view-only everywhere
    FOREACH mod IN ARRAY all_mods LOOP
        INSERT INTO permissions (tenant_id, role_id, module, can_view, can_create, can_edit, can_approve)
        SELECT tid, id, mod, true, false, false, false
        FROM roles WHERE tenant_id = tid AND name = 'Auditor'
        ON CONFLICT (tenant_id, role_id, module) DO NOTHING;
    END LOOP;
END
$perms$;

-- ---------------------------------------------------------------------------
-- 6. Users (one per role; branch matched to branch registry)
-- ---------------------------------------------------------------------------
INSERT INTO users (id, tenant_id, role_id, full_name, email, phone, branch, branch_id, status)
SELECT
    gen_random_uuid(),
    '11111111-1111-1111-1111-111111111111',
    r.id,
    u.full_name,
    u.email,
    u.phone,
    u.branch_name,
    b.id,
    u.status
FROM (VALUES
    ('System Admin',    'admin@genesisprestige.test',    '+254700000001', 'HQ',         'System Admin',     'active'),
    ('Antony Maina',    'antony.maina@dev.test',         '+254700000002', 'Nairobi CBD','Loan Officer',     'active'),
    ('Francis Karingi', 'francis.karingi@dev.test',      '+254700000003', 'Nairobi CBD','Branch Manager',   'active'),
    ('Jane Muthoni',    'jane.muthoni@dev.test',         '+254700000004', 'Thika',      'Teller',           'active'),
    ('Board Member A',  'board.member.a@dev.test',       '+254700000005', 'HQ',         'Credit Committee', 'active'),
    ('Ruth Achieng',    'ruth.achieng@dev.test',         '+254700000006', 'HQ',         'Accountant',       'active'),
    ('Sam Kiptoo',      'sam.kiptoo@dev.test',           '+254700000007', 'HQ',         'Auditor',          'suspended')
) AS u(full_name, email, phone, branch_name, role_name, status)
JOIN roles r ON r.tenant_id = '11111111-1111-1111-1111-111111111111'
    AND r.name = u.role_name
JOIN branches b ON b.tenant_id = '11111111-1111-1111-1111-111111111111'
    AND b.name = u.branch_name
ON CONFLICT (tenant_id, email) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 7. Loan products
-- ---------------------------------------------------------------------------
INSERT INTO loan_products (id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months, guarantors_required, active)
VALUES
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Normal Loan',        12.00, 3.00,  36, 2, true),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Emergency Loan',     14.00, 1.50,  12, 1, true),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Development Loan',   10.00, 4.00,  60, 2, true),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'School Fees Loan',   12.00, 2.00,   9, 1, true),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Business Loan',      13.00, 5.00,  48, 3, true),
    (gen_random_uuid(), '11111111-1111-1111-1111-111111111111', 'Salary Advance',     15.00, 1.00,   3, 0, true)
ON CONFLICT (tenant_id, name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 8. Members (20 members across branches, mixed types and statuses)
-- ---------------------------------------------------------------------------
DO $members$
DECLARE
    tid  uuid := '11111111-1111-1111-1111-111111111111';
    rec  RECORD;
    mid  uuid;
    bid  uuid;
BEGIN
    FOR rec IN
    SELECT * FROM (VALUES
        ('M001', 'person',  'Alice Wanjiku',      '+254711000001', 'alice.wanjiku@dev.test',   'Nairobi CBD', 'active'),
        ('M002', 'person',  'Brian Otieno',        '+254711000002', 'brian.otieno@dev.test',    'Nairobi CBD', 'active'),
        ('M003', 'person',  'Catherine Njoroge',   '+254711000003', 'catherine.njoroge@dev.test','Thika',      'active'),
        ('M004', 'person',  'David Kamau',         '+254711000004', 'david.kamau@dev.test',     'Thika',       'active'),
        ('M005', 'person',  'Esther Auma',         '+254711000005', 'esther.auma@dev.test',     'HQ',          'active'),
        ('M006', 'person',  'Felix Mutua',         '+254711000006', 'felix.mutua@dev.test',     'Mombasa',     'active'),
        ('M007', 'person',  'Grace Chebet',        '+254711000007', 'grace.chebet@dev.test',    'Kisumu',      'active'),
        ('M008', 'person',  'Hassan Omar',         '+254711000008', 'hassan.omar@dev.test',     'Mombasa',     'active'),
        ('M009', 'person',  'Irene Wafula',        '+254711000009', 'irene.wafula@dev.test',    'Nairobi CBD', 'active'),
        ('M010', 'person',  'James Kariuki',       '+254711000010', 'james.kariuki@dev.test',   'HQ',          'arrears'),
        ('M011', 'person',  'Karen Ndung''u',      '+254711000011', 'karen.ndungu@dev.test',    'Nairobi CBD', 'active'),
        ('M012', 'person',  'Lawrence Mwangi',     '+254711000012', 'lawrence.mwangi@dev.test', 'Thika',       'active'),
        ('M013', 'person',  'Mary Akinyi',         '+254711000013', 'mary.akinyi@dev.test',     'Kisumu',      'active'),
        ('M014', 'person',  'Nathan Korir',        '+254711000014', 'nathan.korir@dev.test',    'HQ',          'arrears'),
        ('M015', 'person',  'Olivia Njeri',        '+254711000015', 'olivia.njeri@dev.test',    'Nairobi CBD', 'active'),
        ('M016', 'company', 'Simba Traders Ltd',   '+254722000001', 'simba.traders@dev.test',   'Nairobi CBD', 'active'),
        ('M017', 'company', 'Kilimo Bora Ent.',    '+254722000002', 'kilimo.bora@dev.test',     'Thika',       'active'),
        ('M018', 'group',   'Jua Kali Chama',      '+254733000001', 'juakali.chama@dev.test',   'Nairobi CBD', 'active'),
        ('M019', 'vehicle', 'Matatu KCB 123A',     '+254744000001', 'kbc123a@dev.test',         'Nairobi CBD', 'active'),
        ('M020', 'person',  'Zipporah Muthoni',    '+254711000020', 'zipporah.muthoni@dev.test','HQ',          'exited')
    ) AS t(member_no, type, name, phone, email, branch_name, status)
    LOOP
        SELECT id INTO bid FROM branches
        WHERE tenant_id = tid AND name = rec.branch_name;

        INSERT INTO members (id, tenant_id, member_no, type, name, phone, email, status, branch_id)
        VALUES (gen_random_uuid(), tid, rec.member_no, rec.type, rec.name, rec.phone, rec.email, rec.status, bid)
        ON CONFLICT (tenant_id, member_no) DO NOTHING;
    END LOOP;
END
$members$;

-- ---------------------------------------------------------------------------
-- 9. Share & deposit accounts for all members
-- ---------------------------------------------------------------------------
DO $accounts$
DECLARE
    tid  uuid := '11111111-1111-1111-1111-111111111111';
    rec  RECORD;
BEGIN
    FOR rec IN
        SELECT id FROM members WHERE tenant_id = tid
    LOOP
        INSERT INTO share_accounts (tenant_id, member_id, balance)
        VALUES (tid, rec.id,
            ROUND((RANDOM() * 95000 + 5000)::numeric, 2))
        ON CONFLICT (tenant_id, member_id) DO NOTHING;

        INSERT INTO deposit_accounts (tenant_id, member_id, balance)
        VALUES (tid, rec.id,
            ROUND((RANDOM() * 490000 + 10000)::numeric, 2))
        ON CONFLICT (tenant_id, member_id) DO NOTHING;
    END LOOP;
END
$accounts$;

-- ---------------------------------------------------------------------------
-- 10. Transactions + ledger entries: 14 months of deposits & share top-ups
--     (one deposit + one share top-up per active member per month)
-- ---------------------------------------------------------------------------
DO $txns$
DECLARE
    tid     uuid := '11111111-1111-1111-1111-111111111111';
    rec     RECORD;
    mo      integer;
    txn_id  uuid;
    txdate  timestamptz;
    dep_amt numeric;
    sha_amt numeric;
    dep_ref text;
    sha_ref text;
BEGIN
    FOR rec IN
        SELECT m.id AS mid
        FROM members m
        WHERE m.tenant_id = tid AND m.status IN ('active','arrears')
    LOOP
        FOR mo IN 1..14 LOOP
            txdate  := date_trunc('month', now() - (mo || ' months')::interval)
                       + interval '5 days';
            dep_amt := ROUND((RANDOM() * 9000 + 1000)::numeric, 2);
            sha_amt := ROUND((RANDOM() * 4000 + 1000)::numeric, 2);
            dep_ref := 'DEP-' || extract(year from txdate)::text
                       || lpad(extract(month from txdate)::text, 2,'0')
                       || '-' || substr(rec.mid::text, 1, 8);
            sha_ref := 'SH-' || extract(year from txdate)::text
                       || lpad(extract(month from txdate)::text, 2,'0')
                       || '-' || substr(rec.mid::text, 1, 8);

            -- Deposit
            txn_id := gen_random_uuid();
            INSERT INTO transactions (id, tenant_id, txn_ref, member_id, type, amount, channel, occurred_at)
            VALUES (txn_id, tid, dep_ref, rec.mid, 'deposit', dep_amt, 'mpesa', txdate)
            ON CONFLICT (tenant_id, txn_ref) DO NOTHING;

            INSERT INTO ledger_entries (tenant_id, transaction_id, account, side, amount)
            VALUES
                (tid, txn_id, 'cash',    'debit',  dep_amt),
                (tid, txn_id, 'deposits','credit', dep_amt)
            ON CONFLICT DO NOTHING;

            -- Share top-up
            txn_id := gen_random_uuid();
            INSERT INTO transactions (id, tenant_id, txn_ref, member_id, type, amount, channel, occurred_at)
            VALUES (txn_id, tid, sha_ref, rec.mid, 'share_topup', sha_amt, 'bank', txdate + interval '1 hour')
            ON CONFLICT (tenant_id, txn_ref) DO NOTHING;

            INSERT INTO ledger_entries (tenant_id, transaction_id, account, side, amount)
            VALUES
                (tid, txn_id, 'cash',          'debit',  sha_amt),
                (tid, txn_id, 'share_capital', 'credit', sha_amt)
            ON CONFLICT DO NOTHING;
        END LOOP;
    END LOOP;
END
$txns$;

-- ---------------------------------------------------------------------------
-- 12. Loans: 10 active/closed loans spread across products and members
-- ---------------------------------------------------------------------------
DO $loans$
DECLARE
    tid      uuid := '11111111-1111-1111-1111-111111111111';
    rec      RECORD;
    app_id   uuid;
    loan_id  uuid;
    txn_id   uuid;
    product  RECORD;
    member   RECORD;
    amount   numeric;
    months   integer;
    rate     numeric;
    disb_at  timestamptz;
    mo_rate  numeric;
    balance  numeric;
    inst_pri numeric;
    inst_int numeric;
    i        integer;
    due      date;
    loan_data RECORD;
BEGIN
    FOR loan_data IN
    SELECT * FROM (VALUES
        ('M001', 'Normal Loan',      150000, 24, now() - interval '18 months', 'active'),
        ('M002', 'Emergency Loan',    50000, 12, now() - interval '10 months', 'active'),
        ('M003', 'Development Loan', 300000, 48, now() - interval '30 months', 'closed'),
        ('M004', 'School Fees Loan',  30000,  9, now() - interval '8 months',  'active'),
        ('M005', 'Normal Loan',      200000, 36, now() - interval '24 months', 'active'),
        ('M006', 'Business Loan',    500000, 48, now() - interval '12 months', 'active'),
        ('M007', 'Emergency Loan',    40000, 12, now() - interval '6 months',  'active'),
        ('M008', 'Normal Loan',      120000, 24, now() - interval '20 months', 'closed'),
        ('M010', 'Normal Loan',      180000, 36, now() - interval '15 months', 'active'),
        ('M014', 'Development Loan', 250000, 60, now() - interval '8 months',  'active')
    ) AS t(member_no, product_name, amount, months, disbursed_at, status)
    LOOP
        SELECT id INTO member FROM members
        WHERE tenant_id = tid AND member_no = loan_data.member_no;

        SELECT id, rate_pct INTO product FROM loan_products
        WHERE tenant_id = tid AND name = loan_data.product_name;

        IF member IS NULL OR product IS NULL THEN CONTINUE; END IF;

        amount  := loan_data.amount;
        months  := loan_data.months;
        rate    := product.rate_pct;
        disb_at := loan_data.disbursed_at;

        -- Application
        app_id := gen_random_uuid();
        INSERT INTO loan_applications
            (id, tenant_id, member_id, product_id, amount, term_months,
             rate_pct, purpose, stage, created_at, updated_at)
        VALUES
            (app_id, tid, member.id, product.id, amount, months,
             rate, 'General purpose', 'disbursed', disb_at - interval '14 days',
             disb_at - interval '1 day')
        ON CONFLICT DO NOTHING;

        -- Disbursement transaction
        txn_id := gen_random_uuid();
        INSERT INTO transactions
            (id, tenant_id, txn_ref, member_id, type, amount, channel, occurred_at)
        VALUES
            (txn_id, tid,
             'DISB-' || substr(app_id::text, 1, 8),
             member.id, 'loan_disbursement', amount, 'bank', disb_at)
        ON CONFLICT (tenant_id, txn_ref) DO NOTHING;

        INSERT INTO ledger_entries (tenant_id, transaction_id, account, side, amount)
        VALUES
            (tid, txn_id, 'loans_receivable', 'debit',  amount),
            (tid, txn_id, 'cash',             'credit', amount)
        ON CONFLICT DO NOTHING;

        -- Loan row
        mo_rate := rate / 12 / 100;
        balance := CASE loan_data.status
            WHEN 'closed' THEN 0
            ELSE ROUND(amount * (1 - (months - GREATEST(0,
                EXTRACT(YEAR FROM age(now(), disb_at))::integer * 12 +
                EXTRACT(MONTH FROM age(now(), disb_at))::integer
            ) * 1.0 / months)::numeric), 2)
        END;

        loan_id := gen_random_uuid();
        INSERT INTO loans
            (id, tenant_id, application_id, member_id, product_id,
             principal, balance, rate_pct, term_months, status,
             classification, days_past_due, penalty_due,
             disbursed_at, closed_at, created_at, updated_at)
        VALUES
            (loan_id, tid, app_id, member.id, product.id,
             amount, GREATEST(0, balance), rate, months,
             loan_data.status,
             CASE loan_data.status WHEN 'active' THEN 'normal' ELSE 'normal' END,
             CASE loan_data.member_no WHEN 'M010' THEN 95
                  WHEN 'M014' THEN 120 ELSE 0 END,
             0,
             disb_at,
             CASE loan_data.status WHEN 'closed' THEN disb_at + (months || ' months')::interval ELSE NULL END,
             disb_at, now())
        ON CONFLICT DO NOTHING;

        -- Repayment schedule: use the original principal for interest calc,
        -- not the current outstanding balance (which is 0 for closed loans).
        inst_pri := ROUND(amount / months, 2);
        balance  := amount;   -- reset to full principal for schedule
        FOR i IN 1..months LOOP
            due      := (disb_at + (i || ' months')::interval)::date;
            inst_int := GREATEST(0, ROUND(balance * mo_rate, 2));
            balance  := GREATEST(0, balance - inst_pri);
            INSERT INTO loan_schedules
                (tenant_id, loan_id, installment_no, due_date,
                 principal_due, interest_due, total_due, paid_amount)
            VALUES
                (tid, loan_id, i, due, inst_pri, inst_int,
                 inst_pri + inst_int,
                 CASE WHEN due < now()::date THEN inst_pri + inst_int ELSE 0 END)
            ON CONFLICT DO NOTHING;
        END LOOP;
    END LOOP;
END
$loans$;

-- ---------------------------------------------------------------------------
-- 12. Accounting periods: close the last 14 months, leave current open
--     Must come AFTER transactions/loans so the closed-period trigger does
--     not block the historical inserts above.
-- ---------------------------------------------------------------------------
DO $periods$
DECLARE
    tid   uuid := '11111111-1111-1111-1111-111111111111';
    admin_id uuid;
    mo    integer;
    ps    date;
    pe    date;
BEGIN
    SELECT id INTO admin_id FROM users
    WHERE tenant_id = tid AND email = 'admin@genesisprestige.test';

    FOR mo IN 1..14 LOOP
        ps := date_trunc('month', now() - (mo || ' months')::interval)::date;
        pe := (ps + interval '1 month - 1 day')::date;
        INSERT INTO accounting_periods
            (tenant_id, period_start, period_end, status, closed_by, closed_at)
        VALUES (tid, ps, pe, 'closed', admin_id, ps + interval '5 days')
        ON CONFLICT (tenant_id, period_start) DO NOTHING;
    END LOOP;
END
$periods$;

-- ---------------------------------------------------------------------------
-- 13. One arrears member with an open recovery case
-- ---------------------------------------------------------------------------
DO $recovery$
DECLARE
    tid      uuid := '11111111-1111-1111-1111-111111111111';
    admin_id uuid;
    loan_id  uuid;
BEGIN
    SELECT id INTO admin_id FROM users
    WHERE tenant_id = tid AND email = 'admin@genesisprestige.test';

    -- Pick a loan belonging to M010 (days_past_due = 95 → substandard)
    SELECT l.id INTO loan_id FROM loans l
    JOIN members m ON m.id = l.member_id
    WHERE l.tenant_id = tid AND m.member_no = 'M010' AND l.status = 'active'
    LIMIT 1;

    IF loan_id IS NOT NULL AND admin_id IS NOT NULL THEN
        INSERT INTO recovery_cases
            (tenant_id, loan_id, status, opened_by, assignee_id,
             classification_at_open, days_past_due_at_open, opened_at)
        VALUES
            (tid, loan_id, 'open', admin_id, admin_id,
             'substandard', 95, now() - interval '5 days')
        ON CONFLICT DO NOTHING;
    END IF;
END
$recovery$;

-- ---------------------------------------------------------------------------
-- 14. One completed member exit (M020 — already exited)
-- ---------------------------------------------------------------------------
DO $exit$
DECLARE
    tid    uuid := '11111111-1111-1111-1111-111111111111';
    mid    uuid;
    uid    uuid;
    txn_id uuid;
    exit_id uuid;
BEGIN
    SELECT id INTO mid FROM members WHERE tenant_id = tid AND member_no = 'M020';
    SELECT id INTO uid FROM users   WHERE tenant_id = tid AND email = 'admin@genesisprestige.test';
    IF mid IS NULL OR uid IS NULL THEN RETURN; END IF;

    txn_id  := gen_random_uuid();
    exit_id := gen_random_uuid();

    -- Use now() so the transaction lands in the open (current) period,
    -- not a closed one. The exit still demonstrates the full settled workflow.
    INSERT INTO transactions
        (id, tenant_id, txn_ref, member_id, type, amount, channel, occurred_at)
    VALUES
        (txn_id, tid, 'EXIT-M020', mid, 'exit_settlement', 68500.00, 'bank', now())
    ON CONFLICT (tenant_id, txn_ref) DO NOTHING;

    INSERT INTO member_exits
        (id, tenant_id, member_id, status,
         shares_amount, deposits_amount, loan_balance, fees, net_payable,
         reason, requested_by, decided_at, settled_at, settlement_transaction_id,
         created_at, updated_at)
    VALUES
        (exit_id, tid, mid, 'settled',
         45000.00, 25000.00, 0.00, 1000.00, 69000.00,
         'Relocation', uid,
         now() - interval '5 days',
         now(),
         txn_id,
         now() - interval '10 days',
         now())
    ON CONFLICT DO NOTHING;
END
$exit$;

-- ---------------------------------------------------------------------------
-- 15. Dividend declaration for last financial year
-- ---------------------------------------------------------------------------
DO $dividends$
DECLARE
    tid  uuid := '11111111-1111-1111-1111-111111111111';
    uid  uuid;
    decl_id uuid;
BEGIN
    SELECT id INTO uid FROM users WHERE tenant_id = tid AND email = 'admin@genesisprestige.test';
    IF uid IS NULL THEN RETURN; END IF;

    decl_id := gen_random_uuid();
    INSERT INTO dividend_declarations (
        id, tenant_id, fy_start, fy_end,
        dividend_rate_pct, rebate_rate_pct,
        eligible_members, total_share_basis, total_deposit_basis,
        total_dividend, total_rebate, total_payout,
        status, requested_by, decided_at, distributed_at,
        created_at, updated_at
    ) VALUES (
        decl_id, tid,
        date_trunc('year', now() - interval '1 year')::date,
        (date_trunc('year', now()) - interval '1 day')::date,
        10.00, 3.00,
        18, 1200000.00, 4500000.00,
        120000.00, 135000.00, 255000.00,
        'distributed', uid,
        date_trunc('year', now()) - interval '5 days',
        date_trunc('year', now()) - interval '3 days',
        date_trunc('year', now()) - interval '10 days',
        now()
    ) ON CONFLICT DO NOTHING;
END
$dividends$;

COMMIT;

-- ---------------------------------------------------------------------------
-- Summary
-- ---------------------------------------------------------------------------
DO $summary$
DECLARE
    tid uuid := '11111111-1111-1111-1111-111111111111';
BEGIN
    RAISE NOTICE '=== Seed complete for tenant 11111111-1111-1111-1111-111111111111 ===';
    RAISE NOTICE 'Branches:              %', (SELECT count(*) FROM branches WHERE tenant_id = tid);
    RAISE NOTICE 'Roles:                 %', (SELECT count(*) FROM roles WHERE tenant_id = tid);
    RAISE NOTICE 'Users:                 %', (SELECT count(*) FROM users WHERE tenant_id = tid);
    RAISE NOTICE 'Members:               %', (SELECT count(*) FROM members WHERE tenant_id = tid);
    RAISE NOTICE 'Loan products:         %', (SELECT count(*) FROM loan_products WHERE tenant_id = tid);
    RAISE NOTICE 'Loans:                 %', (SELECT count(*) FROM loans WHERE tenant_id = tid);
    RAISE NOTICE 'Transactions:          %', (SELECT count(*) FROM transactions WHERE tenant_id = tid);
    RAISE NOTICE 'Accounting periods:    %', (SELECT count(*) FROM accounting_periods WHERE tenant_id = tid);
    RAISE NOTICE 'Recovery cases:        %', (SELECT count(*) FROM recovery_cases WHERE tenant_id = tid);
    RAISE NOTICE 'Member exits:          %', (SELECT count(*) FROM member_exits WHERE tenant_id = tid);
    RAISE NOTICE 'Dividend declarations: %', (SELECT count(*) FROM dividend_declarations WHERE tenant_id = tid);
END
$summary$;
, now()) - interval '1 day')::date,
        10.00, 3.00,
        18, 1200000.00, 4500000.00,
        120000.00, 135000.00, 255000.00,
        'distributed', uid,
        date_trunc('year', now()) - interval '5 days',
        date_trunc('year', now()) - interval '3 days',
        date_trunc('year', now()) - interval '10 days',
        now()
    ) ON CONFLICT DO NOTHING;
END
$dividends$;

COMMIT;

-- ---------------------------------------------------------------------------
-- Summary
-- ---------------------------------------------------------------------------
DO $summary$
DECLARE
    tid uuid := '11111111-1111-1111-1111-111111111111';
BEGIN
    RAISE NOTICE '=== Seed complete for tenant 11111111-1111-1111-1111-111111111111 ===';
    RAISE NOTICE 'Branches:              %', (SELECT count(*) FROM branches WHERE tenant_id = tid);
    RAISE NOTICE 'Roles:                 %', (SELECT count(*) FROM roles WHERE tenant_id = tid);
    RAISE NOTICE 'Users:                 %', (SELECT count(*) FROM users WHERE tenant_id = tid);
    RAISE NOTICE 'Members:               %', (SELECT count(*) FROM members WHERE tenant_id = tid);
    RAISE NOTICE 'Loan products:         %', (SELECT count(*) FROM loan_products WHERE tenant_id = tid);
    RAISE NOTICE 'Loans:                 %', (SELECT count(*) FROM loans WHERE tenant_id = tid);
    RAISE NOTICE 'Transactions:          %', (SELECT count(*) FROM transactions WHERE tenant_id = tid);
    RAISE NOTICE 'Accounting periods:    %', (SELECT count(*) FROM accounting_periods WHERE tenant_id = tid);
    RAISE NOTICE 'Recovery cases:        %', (SELECT count(*) FROM recovery_cases WHERE tenant_id = tid);
    RAISE NOTICE 'Member exits:          %', (SELECT count(*) FROM member_exits WHERE tenant_id = tid);
    RAISE NOTICE 'Dividend declarations: %', (SELECT count(*) FROM dividend_declarations WHERE tenant_id = tid);
END
$summary$;
