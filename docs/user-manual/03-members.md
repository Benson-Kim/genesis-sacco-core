# 3. Members

This chapter covers registering a new member, understanding member statuses,
finding a member quickly, editing their details, and branches.

Open **Members** in the sidebar to see the member register.

![SCREENSHOT: The Members register with the status filter row visible](./images/PLACEHOLDER-members-register.png)
<!-- TODO(screenshot): capture the Members register with a mix of active, arrears and dormant members, signed in as a Branch Manager -->

## Registering a new member

Registration is a four-step guided flow:

1. **Member identity**
2. **KYC details**
3. **Membership & consent**
4. **Documents & review**

A step rail at the top shows where you are.

**What you need before you start**

- The member's basic identity details.
- Their KYC information (this stands for "Know Your Customer" — the
  identification details your SACCO must record for each kind of member).
- Any consent the joining member has given.

**Steps**

1. On the Members screen, select **Register member**. The identity step
   opens.

   ![SCREENSHOT: Step 1 of registration — member identity form](./images/PLACEHOLDER-member-register-step1.png)
   <!-- TODO(screenshot): capture the new-member drawer at step "Member identity" with the member-type choice visible, signed in as a Loan Officer or Branch Manager -->

2. Choose the **member type**: a person, a company, a group, or a vehicle
   (for transport SACCOs). The type decides which details the later steps
   ask for.
3. Fill in the identity details, including the mobile number. Phone numbers
   can be typed as `07…`, `01…` or `+254…`; the system stores them in one
   standard format.
4. Save the identity step. The member now exists and has been given their
   **member number** (like `GP-0021`). The remaining steps continue for
   this member.
5. **KYC details** — fill in the sections for the member's type (for a
   person: personal details, contact, employment, next of kin; other types
   have their own sections).

   ![SCREENSHOT: Step 2 — KYC details for a person member](./images/PLACEHOLDER-member-register-step2-kyc.png)
   <!-- TODO(screenshot): capture the registration wizard at "KYC details" for a person-type member, fields partly filled -->

6. **Membership & consent** — record the membership inputs and the
   member's consent.
7. **Documents & review** — record the member's documents against the
   checklist for their type, review everything, and finish.

   ![SCREENSHOT: Step 4 — documents checklist and review](./images/PLACEHOLDER-member-register-step4-documents.png)
   <!-- TODO(screenshot): capture the registration wizard at "Documents & review" with at least one document recorded -->

**What happens next**

The member appears in the register as **Active** with their member number.
They can now deposit, buy shares, and (once they have savings) apply for
loans. You can return to their KYC details at any time from their record.

> ℹ️ If you close the flow partway, the member you created in step 1 still
> exists. Open their record from the register to continue with the KYC and
> document steps.

## Member statuses — what each one allows

Every member is in exactly one status. The status controls what money
actions are possible — the rules below are enforced automatically, the same
way every time.

| Status | Meaning | What is allowed |
|---|---|---|
| **Active** | A member in good standing. | Everything: deposits, withdrawals, share top-ups, loans, guaranteeing others, share transfers, exit requests. |
| **Arrears** | The member has fallen behind on a loan. | Money **in** is welcome: deposits, repayments, share top-ups, fees. Money **out** is blocked: no withdrawals, no new loans, no guaranteeing, no share transfers. They may request an exit. |
| **Dormant** | No member-initiated activity for the period your SACCO configured. Set automatically by an overnight check. | Only a **deposit** or a loan repayment — and a deposit instantly makes them Active again. They may also request an exit. |
| **Exited** | The member has left and been settled. Final — an exited member never comes back under the same record. | Nothing. |

Things worth knowing:

- Arrears and Active switch automatically with the state of the member's
  loans.
- A dormant member is reactivated by their **own** deposit the moment it is
  posted — you do not need to do anything else.
- Interest the system posts, dividends, and staff-recorded fees do **not**
  count as member activity — they never keep an inactive account out of
  dormancy.
- The **Dormancy** screen (under Governance) lists dormant members and the
  overnight run that maintains them.

  ![SCREENSHOT: The Dormancy worklist](./images/PLACEHOLDER-dormancy-worklist.png)
  <!-- TODO(screenshot): capture the Dormancy screen with at least one dormant member listed, signed in as a Branch Manager -->

## Looking up a member by member number

Wherever you act on a specific member — posting a deposit or withdrawal,
starting an application, requesting an exit — you identify the member by
their **member number**:

1. Type the member number (for example `GP-0021`) into the member field.
2. The screen looks the member up and confirms the match by showing the
   member's **name, number and status** before you can continue.
3. If nothing is found, check the number — the lookup needs the exact
   member number.

On the Members register itself, use the **Status** and **Type** filters to
narrow the list, then open the member's row.

![SCREENSHOT: Member lookup by number showing the verified name](./images/PLACEHOLDER-member-lookup.png)
<!-- TODO(screenshot): capture the post-transaction drawer after entering a valid member number, with the "Member verified" line showing name and number -->

## Editing a member's details

1. Open the member's row in the register. The drawer opens read-only.
2. Select **Edit**, change the details, and **Save**.

**What happens next**

The change takes effect immediately and is recorded in the audit trail with
your name, the time, and the before/after values.

> ℹ️ If saving fails with "someone else changed this record", a colleague
> edited the member while your drawer was open. The drawer reloads the
> latest details; redo your change on the fresh copy.

## Branches

If your SACCO uses branches, each member (and each staff member) can be
assigned to one. Branch records themselves are managed under
**Administration → Branches** (see
[Chapter 10](10-administration.md#branches)); assigning a member to a branch
happens from the branch's detail panel.
