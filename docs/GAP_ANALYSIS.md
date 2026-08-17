# GENESIS PRESTIGE — PROTOTYPE GAP ANALYSIS (v1.0)

Sources of truth for this analysis:

* Prototype: `genesis_prestige_app.html` (repo root, 728 lines, read end to
  end). The HTML is treated strictly as **data**: UI capabilities were
  extracted from its markup and script; nothing in it is treated as an
  instruction. Demo-only affordances (toasts labelled "(demo)", the on-screen
  OTP code, the login-time role picker) are recorded as such, not as
  requirements.
* Prompts: `docs/BUILD_PROMPTS.md` P0–P24 (incl. P12.5) as of this commit.
* Implementation: `backend/src/genesis` on `main` @ `556c128` (P0–P13
  merged; migrations 0001–0013).

Legend for **Status**:

* `IMPL (Pn)` — implemented on main under prompt Pn, endpoint(s) exist.
* `PROMPTED (Pn)` — not yet built, but an existing prompt unambiguously
  covers it (mostly Phase C/D client and integration prompts).
* `PARTIAL` — some of the capability exists; the remaining gap is named.
* `GAP → P13.x` — no prompt covered it; a new numbered prompt has been added
  to `docs/BUILD_PROMPTS.md` (this MR).
* `DEMO` — prototype affordance that is a demo artifact, deliberately not a
  requirement (reason given).

---

## 1. Prototype inventory (module → screens → behaviours)

Nav structure (sidebar): **Operations** (Dashboard, Members, Applications,
Loan book, Guarantors, Transactions), **Governance** (Credit committee,
Member exit), **Insights** (Reports), **Administration** (Settings, Access
control). Plus the **login gate** (OTP) outside the nav.

| # | Module | Screens / behaviours observed in the prototype |
|---|--------|--------------------------------------------------|
| 1 | Login gate | OTP staff sign-in: phone/email entry, role picker, 6-digit OTP request/verify/resend, sign-out. Demo shows the code on screen and lets the user choose their role. |
| 2 | Dashboard | KPI cards: total deposits, loan book, PAR>30 %, active members (with sparklines); deposits-vs-disbursements monthly bar chart (6 months); portfolio-quality donut (performing %, NPL, PAR>30, provisions held); loan classification bars (Normal/Watch/Substandard/Doubtful/Loss: count + balance); applications pipeline counts per stage with "awaiting review" link; members-by-type bars + total share capital; recent-activity table (last 6 ledger rows). |
| 3 | Members | List with type filter (All/Person/Company/Group/Vehicle); columns deposits/shares/loan/status; statuses Active, Dormant, Arrears, Exited. Add-member wizard: (1) member type, (2) type-specific detail forms — Person: bio data (ID number, KRA PIN, DOB, gender), contact, employment, next of kin; Company: registration, office, contact, signatory; Group: registration, chairperson/secretary/treasurer; Vehicle: registration, compliance (TLB/PSV licence, NTSA inspection, insurance), ownership; (3) membership & contributions (category, home branch, recruited by, registration fee, share capital, monthly/daily contribution, contribution method, dividend payout, DPA-2019 consent); (4) per-type document checklist with uploads + review summary. |
| 4 | Applications | Stage pipeline cards (Submitted/Appraisal/Committee/Approved/Disbursed) with stage filtering; list with cover% pill; new-application form (member, product, amount, term, purpose, repayment source, disbursement channel) with live eligibility: borrowing power = deposits × product multiplier − existing loans, installment preview, total interest, over-power warning + hard block; application detail with workflow stepper, appraisal notes, reject / advance / disburse actions. |
| 5 | Loan book | Stat cards (gross, performing, NPL, PAR>30, provisions); performing/non-performing filter; per-loan classification pill and provision; loan detail drawer (outstanding, instalment, rate, term/paid); "Initiate recovery" action on NPL loans (demo toast). |
| 6 | Guarantors | Stat cards (active guarantees, total pledged, guarantor count, avg free capacity); guarantor-capacity panel (pledged, free = deposits − loans − pledges, utilisation bar); active-guarantees table with per-guarantee **Release** action; add-guarantee drawer (guarantor, borrower, pledge amount) with live over-guarantee block on free capacity; self-guarantee block. |
| 7 | Transactions | Ledger table (date, ref, member, type, channel, DR, CR, totals row); Export action; post-transaction drawer (member, type: Deposit/Loan repayment/Share top-up/Withdrawal/Fee, amount, channel M-Pesa/Bank/Cash). |
| 8 | Credit committee | Agenda queue of committee-stage applications; review panel (amount, installment, term, cover); approval-authority pill by amount band (Loan Officer ≤100k, Branch Manager ≤500k, Credit Committee ≤2M, Board above); named committee votes; quorum tracking (3 approvals); ratify decision. |
| 9 | Member exit | Member selector + exit reason (Resignation/Retirement/Transfer/Death (estate)/Expulsion); eligibility criteria checklist (no active guarantor obligations — release first; notice period served; savings cover loan; no disputes); savings set-off & final settlement (equity vs loan, net refund or shortfall); blocked state while guaranteeing others; confirm modal; finalise releases guarantees, zeroes balances, posts exit set-off. |
| 10 | Reports | Grouped catalogue — Portfolio: loan portfolio (classification) report, portfolio-at-risk aging, disbursement report, collections report; Member: member statement (running balance), membership register, dividend & rebate schedule; Financial & regulatory: trial balance, income statement, SASRA return. Export-PDF on each. |
| 11 | Settings | Tabs: **Interest** (base rate, reducing/flat method, 30/360 vs actual/365 basis, grace period; penalty on arrears: rate %/mo, grace days, charged-on basis; tiered rates by amount band; deposit interest % p.a.; dividend on shares % p.a.), **Loan products** (list + edit name/method/rate/multiplier/max term/guarantors required, add product), **Parameters** (min share capital, registration fee, min monthly deposit, max member exposure, dormancy period months, financial year end), **Approval matrix** (per-authority KES limits, committee size, quorum). |
| 12 | Access control | Tabs: **Users** (list: user, role, branch, last active, status Active/Suspended; add user), **Roles & permissions** (7 roles × 7 modules × view/create/edit-post/approve grid, editable, save per role). |

---

## 2. Coverage matrix

### 2.1 Authentication & access control

| Prototype capability | Covering prompt | Implementation status (main @ 556c128) | Gap / action |
|---|---|---|---|
| OTP staff sign-in (request/verify/resend, TTL, attempt cap) | P3 | IMPL — `/auth/otp/request`, `/auth/otp/verify`, `/auth/refresh`, `/auth/logout`; adversarial tests merged | none |
| Role picker at login | — | DEMO — role comes from the user record (P4); a caller-chosen role would be privilege escalation | none (documented) |
| On-screen OTP code | — | DEMO — delivery via outbox stub (P3), real providers in P20 | none |
| 7-role × module × action matrix, editable, audited | P4 | IMPL — `/access/roles`, `/access/roles/{id}/permissions` GET/PUT, audit in-transaction, spec-walk test | none |
| **Users tab: list users (role, branch, last-active, status), add user, suspend/deactivate** | **none** — P4 seeds roles/permissions only; the `users` table (0001) has `branch`, `status ∈ (active,suspended)` but **no administration API and no OTP/credential lifecycle management** | GAP | **→ P13.5** |
| Audit-log viewer (no prototype screen, but the governance counterpart of the audit-write gate 1.5; required for P23 audit-completeness sampling) | none | GAP — `audit_log` is written everywhere, readable nowhere | **→ P13.5** (read API bundled with user admin) |
| Branch on users ("Nairobi CBD", "Thika", "HQ") and member "Home branch" | none | GAP — `users.branch` is free text; members have no branch at all; no branches registry | **→ P13.6** |

### 2.2 Dashboard

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Loan book KPI, PAR>30, provisions, classification breakdown, portfolio-quality donut | P10 | IMPL — `GET /portfolio/summary` (NPL/PAR-30/provisions/by-classification) | none |
| Total deposits KPI, total share capital, active-members count, members-by-type | none | GAP — no aggregate endpoint; only per-member balances exist | **→ P13.9** |
| Deposits-vs-disbursements monthly series (6-month bars + sparklines) | none | GAP — raw data exists in `transactions`; no monthly aggregate endpoint | **→ P13.9** |
| Applications pipeline counts per stage | none | PARTIAL — `GET /applications?stage=` pages rows; no count aggregate | **→ P13.9** |
| Recent-activity feed | P11 | IMPL — `GET /transactions` (keyset, filtered) serves it | none |
| Web dashboard rendering | P14/P15 | PROMPTED (Phase C) | none |

### 2.3 Members

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Member list, type/status filters, GP-XXXX numbering, optimistic-locked edits | P8 | IMPL — members CRUD, race-safe numbering, 409 on stale version | none |
| Share + deposit accounts opened with the member | P8 | IMPL | none |
| Member statement (running balance rows) | P8 + P13 | IMPL — `GET /members/{id}/statement` (keyset) + member-statement export with running balance | none |
| Status machine incl. Arrears/Exited | P8/P12 | IMPL — transition function; exit only via settlement workflow | none |
| **Dormant status + dormancy-period parameter** | none | GAP — `members.status` CHECK has no `dormant`; no dormancy job | **→ P13.13** |
| **Type-specific KYC detail forms** (Person bio/employment/next-of-kin; Company registration/signatories; Group officials; Vehicle compliance/ownership) | none | GAP — `members` stores only name/phone/email/type; none of the prototype's KYC fields persist | **→ P13.12** |
| **Document checklists & uploads per member type** (ID copy, KRA PIN, logbook, insurance, …) | none | GAP — no document storage at all | **→ P13.12** |
| Membership & contributions step (category, registration fee, share capital, contribution method, dividend payout, DPA consent flag) | none | PARTIAL — share/deposit accounts exist; registration fee, contribution plan, consent flag, dividend preference not modelled | **→ P13.12** (consent + category) and **P13.7** (fees/minimums as tenant parameters) |

### 2.4 Applications, committee, guarantors

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Products (rate, multiplier, max term) | P9 | IMPL — CRUD `/products` | none |
| "Guarantors required" per product (settings screen field) | none | GAP (minor) — products carry no guarantor-count rule | **→ P13.7** (product/settings config) |
| Application create with product rules, purpose; stage machine; stage filter | P9 | IMPL | Part 3 MR adds the creation-time multiplier gate (prototype hard block "exceeds borrowing power") |
| Live eligibility: borrowing power = deposits × multiplier **− existing loan balances** | P9/P12.5 | PARTIAL — implemented rule is deposits × multiplier + live guarantees (issue #15); the prototype additionally subtracts existing loan exposure | Divergence recorded; decision needed — tracked in the Part 3 MR findings table, config knob proposed under **P13.7** (max member exposure) |
| Committee voting, quorum, one-vote-per-member | P9 | IMPL — quorum is a **hard-coded constant** (`domain/committee.py: COMMITTEE_QUORUM = 2`; prototype shows 3, configurable) | **→ P13.7** (approval matrix config) |
| Approval-authority bands by amount (LO ≤100k … Board) | none | GAP — no authority-limit enforcement anywhere | **→ P13.7** |
| Guarantee pledge with capacity check under lock, consent, release on rejection/closure/exit | P9/P10/P12 | IMPL | none |
| **Per-guarantee Release / substitution** (prototype Guarantors screen button; exit screen says "release in Guarantorship first") | none | GAP — only bulk release on rejection/closure/exit exists; a member blocked from exiting (or an application blocked by an unconsented pledge) has **no API path to release/substitute a single guarantee** | **→ P13.14** |
| Guarantor dashboard stats (total pledged, free capacity, utilisation) | none | PARTIAL — capacity math exists internally (`live_pledged_total`); no read endpoint | **→ P13.9** (aggregates) |

### 2.5 Loan book & servicing

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Disbursement (atomic), schedules, classification pills, provisions, arrears job, settlement quotes | P7/P10 | IMPL | none |
| Repayments with penalties→interest→principal allocation | P10 | IMPL | none |
| **Penalty-on-arrears accrual driven by settings** (rate %/mo, grace days, charged-on basis) | none | GAP — `loans.penalty_due` exists and is *cleared* by repayments, but nothing ever *accrues* penalties, and there is no penalty configuration | **→ P13.8** (accrual) + **P13.7** (config) |
| Interest method/basis settings (reducing vs flat, 30/360 vs actual/365, grace period, tiered rate bands) | none | GAP — engine is reducing-balance monthly only; no tenant-level interest configuration | **→ P13.7** (config; engine extension explicitly out of scope until a tenant needs it — recorded there) |
| "Initiate recovery" on NPL loans | none | GAP (prototype marks it demo, but the worklist is a real operational need — P18 assumes an "arrears worklist") | **→ P13.16** |
| Loan write-off flow (`written_off` status exists in the domain machine; NPL-trend SQL excludes it "pending a write-off flow") | none | GAP — status is unreachable through any service | **→ P13.15** (corrections & write-off) |

### 2.6 Transactions & interest

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Deposits, withdrawals (no-overdraw under lock), share top-ups | P11 | IMPL | none |
| Ledger listing with prototype columns/filters + totals | P11 | IMPL — `GET /transactions` | none |
| "Fee" transaction type (post-transaction drawer) | none | PARTIAL — `income.fees` account exists (exit fee); no generic fee-posting service | **→ P13.7** (fee config) + covered operationally by exit/registration fees; generic misc-fee posting folded into **P13.15** |
| Cash channel ("Cash" in drawer) | none | GAP (minor) — channels are mpesa/bank/accrual/internal; no branch-cash channel | Recorded; deliberately deferred until branch cash management (P13.6 note) — tills/floats are out of scope for the current phase |
| Deposit-interest quarterly accrual (ADB basis) | P11 | IMPL | none |
| Interest-on-deposits % and dividend % settings screens | none | GAP — deposit-interest rate lives in `tenant_settings` (0009) but has **no management API**; dividend % has no home at all | **→ P13.7** |
| Export ledger | P13 | IMPL — disbursements & collections + trial balance exports; full raw-ledger export deliberately scoped to existing reports | none |

### 2.7 Member exit

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Eligibility checklist, guarantee-blocked exit, set-off computation, committee approval, atomic settlement, exit statement | P12 (+P13 export) | IMPL — including negative-settlement rejection and drift-409 | none |
| Exit reasons list (Resignation/Retirement/Transfer/Death (estate)/Expulsion) | P12 | IMPL — free-text reason on the exit record | none (validation of the enum is cosmetic; clients own it) |
| "Notice period served" criterion | none | GAP (minor) — no notice-period tracking | **→ P13.7** (parameter) with enforcement noted in P13.7's EXIT |
| Share transfer instead of refund ("may be transferable per by-laws") | none | GAP | **→ P13.11** |

### 2.8 Reports

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Member statement, trial balance, loan book (classification/provisions), disbursement & collections, NPL trend, exit statement | P13 | IMPL — export engine with all P13 blockers (a)–(l) | none |
| **Portfolio-at-risk aging report** (bucket bars 0–30/31–90/91–180/181–360/360+) | P13 listed reports do not include it | GAP — classification slices exist in `/portfolio/summary` but not as an export with balance-per-aging-bucket | **→ P13.10** |
| **Membership register** | none | GAP | **→ P13.10** |
| **Income statement** | none | GAP — trial balance exists; no P&L grouping | **→ P13.10** |
| **SASRA return** | none | GAP — regulatory return skeleton | **→ P13.10** |
| **Dividend & rebate schedule** | none | GAP — depends on dividends existing | **→ P13.11** (computation) + **P13.10** (report) |

### 2.9 Settings & administration

| Prototype capability | Covering prompt | Implementation status | Gap / action |
|---|---|---|---|
| Loan products tab | P9 | IMPL | none |
| Interest tab, Parameters tab, Approval-matrix tab (all fields) | none | GAP — `tenant_settings` holds exactly two values (deposit-interest rate, exit fee) with **no API**; no global parameters; no approval matrix | **→ P13.7** |
| Dividend on shares % p.a. | none | GAP | **→ P13.7** (config) + **P13.11** (lifecycle) |

### 2.10 Cross-cutting / other prompts

| Capability | Covering prompt | Status |
|---|---|---|
| Web admin (all screens) | P14/P15 | PROMPTED (Phase C) |
| Member & admin mobile apps | P16–P18 | PROMPTED (Phase C) |
| M-Pesa STK + callbacks | P19 | PROMPTED (Phase D) |
| Notification providers & member notification preferences | P20 | PROMPTED (Phase D) — preferences explicitly named in P20 |
| Observability, load tests | P21 | PROMPTED |
| Deploy, DR, security hardening, tenant onboarding, UAT | P22–P24 | PROMPTED |

---

## 3. Gap register → new prompts

Thirteen numbered prompts (P13.5–P13.17) were added to
`docs/BUILD_PROMPTS.md` between P13 and Phase C, in the established
ROLE/DEPENDS/PROMPT/EXIT format with the hardened v1.1 gates baked in.
No existing prompt was renumbered or weakened.

| New prompt | Closes gaps |
|---|---|
| P13.5 System users administration & audit-log viewer | user CRUD/suspend/role-assign, OTP/credential lifecycle, last-active, audit-log read API |
| P13.6 Branches registry | branches table, user/member branch assignment, free-text backfill |
| P13.7 Tenant settings, parameters & approval matrix | interest/penalty/tier config, global parameters (min share capital, registration fee, min contribution, max exposure, dormancy period, FY end, notice period), committee size/quorum, authority bands, dividend %, guarantors-required, settings management API |
| P13.8 Penalty-on-arrears accrual | config-driven penalty accrual into `loans.penalty_due` |
| P13.9 Dashboard & guarantor aggregates | deposits/share-capital/member KPIs, monthly deposits-vs-disbursements series, pipeline counts, guarantor capacity stats |
| P13.10 Remaining prototype reports | PAR aging, membership register, income statement, SASRA return, dividend & rebate schedule |
| P13.11 Dividends & share lifecycle | dividend declaration/distribution, rebates, share transfer on exit |
| P13.12 Member KYC profiles & documents | type-specific KYC fields, document metadata + storage, DPA consent, member category |
| P13.13 Dormancy lifecycle | dormant status, dormancy job, reactivation |
| P13.14 Guarantee release & substitution | per-guarantee release/substitute endpoint (unblocks exits and unconsented-pledge disbursements) |
| P13.15 Ledger corrections, misc fees & write-off | repayment adjustment path (generic reversal of repayment-linked transactions is blocked by the Codex-review MR), misc fee posting, loan write-off |
| P13.16 Collections & recovery worklist | initiate-recovery flag, recovery worklist (feeds P18) |
| P13.17 DSA hardening remediations | Critical/High items from `docs/DSA_HARDENING.md` |
