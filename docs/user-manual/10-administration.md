# 10. Administration

This chapter is for administrators and branch managers: staff accounts and
roles, the permission matrix, approval limits, SACCO settings, branches,
and the audit trail. These screens live in the **Administration** section
at the bottom of the sidebar.

## Roles — who can do what, in plain language

Every staff account has exactly one **role**, and the role decides what
its holder can see and do. The system starts with seven roles:

| Role | In plain words |
|---|---|
| **System Admin** | Can do everything, including managing staff accounts, permissions and settings. Keep this role rare. |
| **Branch Manager** | Runs day-to-day operations: members, applications, transactions, reports. Approves corrections and share transfers. Manages member identity matters. Cannot approve inside Settings or Access control. |
| **Loan Officer** | Registers members and creates and works loan applications. Sees the operational screens but does not post money and does not approve. |
| **Teller** | The counter role: sees members and transactions, posts deposits, withdrawals and share top-ups. Nothing else. |
| **Credit Committee** | Reviews and votes: loan applications, exits, write-offs, dividend declarations. Sees applications, the loan book and reports. |
| **Accountant** | The books role: posts and manages transactions, sees members, loans and reports, and **proposes** corrections and fees (someone else approves them). |
| **Auditor** | Sees **everything, changes nothing**. Deliberately excluded from approving anyone else's work — reviewing and doing are kept separate. |

Everything not explicitly granted is denied. The server enforces this on
every single action, so hiding or showing buttons in the browser is only a
convenience — permissions cannot be bypassed.

## Staff accounts

Open **Access control** to manage staff.

![SCREENSHOT: The users list](./images/PLACEHOLDER-users-list.png)
<!-- TODO(screenshot): capture the Access control users screen with several users in active and suspended states, signed in as a System Admin -->

### Creating a user

1. Select the create action and fill in the **full name**, **email**, the
   **role**, and optionally the **phone** and **branch**.
2. Save. The person can now sign in with a one-time code sent to their
   registered email or phone ([Chapter 1](01-getting-started.md)).

### Suspending and reactivating

Open the user and change their status. Suspension takes effect
**immediately** — the person's current session dies within moments, their
pending sign-in codes are voided, and they cannot sign in again until
reactivated.

### Changing someone's role

Open the user and assign the new role. It applies to everything they do
from then on.

> ⚠️ The system will not let you suspend or re-role the **last active
> System Admin** — someone must always be able to administer the system.

## The permission matrix

Also under **Access control**: pick a role to see its permission grid —
one row per area of the system, with **view / create / edit / approve**
switches.

![SCREENSHOT: The permission matrix for one role](./images/PLACEHOLDER-permission-matrix.png)
<!-- TODO(screenshot): capture the Permissions screen with the Teller role selected, showing the module-by-action grid, signed in as a System Admin -->

- Ticking and unticking edits a draft; nothing changes until you **save**.
- Changes apply to everyone holding that role.

> ⚠️ Think twice before widening the **Corrections** column — it is the
> money-fixing channel and is deliberately narrow: proposers and approvers
> are separated, and auditors are excluded regardless of any grant.

## Approval limits

Under **Settings**, the approval tab lets you set an **approval matrix**:
a ladder of amounts, each rung naming the role allowed up to that amount.
For example: Branch Manager up to 100,000; Credit Committee up to
1,000,000; amounts above the top rung are decided outside the system.

![SCREENSHOT: The approval limits settings](./images/PLACEHOLDER-approval-limits.png)
<!-- TODO(screenshot): capture the Settings screen approval tab with a configured band ladder, signed in as a System Admin -->

Points to note:

- The ladder must be complete and in increasing order — the screen guides
  you.
- Once a matrix is configured, it caps **every** role. A role you forgot
  to list is held to the **lowest** rung, never accidentally unlimited.
- The limits apply wherever money is ratified: advancing and approving
  loan applications, committee votes, and correction approvals.

## SACCO settings

The **Settings** screen holds every configurable figure, in three tabs:

| Tab | Contains |
|---|---|
| **Interest** | Deposit interest rate, dividend and rebate rates, late-penalty rate, grace days, and what the penalty is charged on. |
| **Parameters** | Minimum share capital, registration fee, minimum monthly contribution, dormancy period, financial-year end month, exit notice period, exit fee. |
| **Approval** | Committee size and quorum, and the approval matrix above. |

![SCREENSHOT: The Settings screen on the Parameters tab](./images/PLACEHOLDER-settings-parameters.png)
<!-- TODO(screenshot): capture the Settings screen on the Parameters tab with values filled, signed in as a System Admin -->

Every value has sensible bounds the screen enforces. Because staff screens
never ask for rates or fees, **these settings are the only place money
parameters come from** — change them here and every future posting uses
the new value; nothing already posted changes.

## Branches

Open **Branches** to manage the branch registry: create branches, edit
their details, and assign members and staff to a branch from the branch's
panel.

![SCREENSHOT: The branches registry](./images/PLACEHOLDER-branches-registry.png)
<!-- TODO(screenshot): capture the Branches screen with at least two branches, one open in the detail drawer showing the roster -->

## The audit trail

Open **Audit log**. Every change anyone makes — every posting, edit,
approval, vote, settings change — is recorded automatically with **who,
what, when**, and the before/after values. Nothing can be added to it by
hand and nothing can ever be removed.

![SCREENSHOT: The audit log with filters](./images/PLACEHOLDER-audit-log.png)
<!-- TODO(screenshot): capture the Audit log screen with entries listed and the entity/action/actor/date filters visible, signed in as an Auditor -->

Filter by the kind of record, the action, the person, or a date range.
Open an entry to see the exact values before and after the change — this
is where the precise figures live for refusals that deliberately show no
figures on screen (for example a refused withdrawal).
