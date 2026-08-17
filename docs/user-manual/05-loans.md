# 5. Loans

This chapter follows a loan from application to final repayment: products,
applying on a member's behalf, guarantors, the committee review, approval
limits, paying out, repayments, and what happens when a loan falls behind.

## Loan products

Every loan is built on a **product** — a template your SACCO configures
with the interest rate and the allowed term. When you create an
application you pick a product; the rate always comes from the product,
never from anything typed into the application.

## Applying on a member's behalf

Open **Applications** in the sidebar.

![SCREENSHOT: The Applications register with stage filters](./images/PLACEHOLDER-applications-register.png)
<!-- TODO(screenshot): capture the Applications register with applications in several stages, signed in as a Loan Officer -->

**What you need before you start**

- The member's member number. The member must be **active** — members in
  arrears or dormancy cannot borrow.
- The product, the amount, and the term in months.

**Steps**

1. Select the button to create a new application.
2. Enter the **member number**; the panel confirms the member's name.
3. Choose the **loan product**, enter the **amount (KES)** and the **term
   (months)**, and optionally the purpose.

   ![SCREENSHOT: The new-application form filled in](./images/PLACEHOLDER-application-create.png)
   <!-- TODO(screenshot): capture the "New loan application" drawer with product chosen and amount/term filled, signed in as a Loan Officer -->

4. Submit. The application starts in the **Submitted** stage.

**What happens next**

The application moves through fixed stages, always in this order:

> **Submitted → Appraisal → Committee → Approved → Disbursed**

It can be **Rejected** at any point up to and including committee review.
Rejected and Disbursed are final.

## How much can a member borrow?

The system computes a ceiling for every application:

> the member's savings × the product's multiplier, **plus** live guarantees
> from other members.

The committee sees this figure while reviewing, and the payout step
enforces it strictly. If the requested amount is above the ceiling, the
member needs more savings or more guarantors.

## Guarantors and pledges

Other members can back an application by pledging part of their savings.
Open **Guarantors** in the sidebar.

![SCREENSHOT: The Guarantors screen with a pledged guarantee](./images/PLACEHOLDER-guarantors-screen.png)
<!-- TODO(screenshot): capture the Guarantors screen with at least one guarantee in "pledged" and one in "active" status -->

- **Pledging.** A guarantor must be an **active** member. Their pledge
  capacity is their savings **minus** what they have already pledged
  elsewhere — the system never lets a member over-pledge, even if two staff
  pledge at the same moment.
- **Consent.** A pledge only becomes an **active** guarantee once the
  guarantor consents. Where the guarantor cannot consent in the system
  themselves (for example consent was given on paper), a staff member with
  the right permission records the consent on their behalf, citing the
  paper consent.
- **While a pledge is live**, that portion of the guarantor's savings is
  locked: they cannot withdraw it.
- **Release and substitution.** A guarantee can be released, or one
  guarantor substituted for another, from the same screen — including after
  the loan has been paid out.

> ⚠️ Explain to guarantors what they are signing up for: their pledged
> savings stay locked until the guarantee is released.

## Committee review

Open **Credit committee** in the sidebar.

![SCREENSHOT: The committee screen with an application open for voting](./images/PLACEHOLDER-committee-screen.png)
<!-- TODO(screenshot): capture the Credit committee screen with one application in the committee stage and the vote buttons visible, signed in as a Credit Committee member -->

The flow:

1. **Recommend.** Moving an application from appraisal into committee is
   the recommendation. The person who does it is recorded as the
   **recommender**.
2. **Vote.** Committee members vote **approve** or **reject**. Each person
   votes once per application — the buttons stay spent after your vote.
3. **Quorum.** The application is decided as soon as enough votes gather on
   one side (your SACCO configures how many). If both sides somehow reach
   the mark together, the application is **rejected** — when in doubt, the
   system says no.
4. **Decision.** Approval freezes the application's amount and terms; a
   rejection is final.

**Who may vote — and why a recommender cannot.** The person who recommended
an application to committee may **not** vote on it. One person should never
both propose and decide a credit decision — the system refuses the
recommender's vote automatically, and the screen tells you when that is
why a vote button is unavailable.

## Approval limits by role

Your SACCO can configure an **approval matrix**: each role gets a largest
amount it may approve. Advancing an application towards approval — and
voting on it — is refused if the amount is above your role's limit, even if
you otherwise have permission. Amounts above the highest configured limit
need a decision outside the system (for example the board). See
[Chapter 10](10-administration.md#approval-limits) for how administrators
configure the limits.

## Paying out (disbursement)

Once approved, the loan is paid out from the application's detail panel.

**What you need before you start**

- The channel (M-Pesa or bank) the money will leave on, and the payout's
  receipt reference.

**Steps**

1. Open the approved application and choose the payout action.
2. Pick the channel, enter the reference, and confirm through the typed
   confirmation.

   ![SCREENSHOT: The disbursement confirmation](./images/PLACEHOLDER-disburse-dialog.png)
   <!-- TODO(screenshot): capture the disbursement dialog on an approved application, before confirming -->

**What happens next**

- The money leaves the SACCO's books, the loan appears in the **Loan book**
  with its full repayment schedule, and the application stage becomes
  **Disbursed**.
- The eligibility ceiling is re-checked at this exact moment — if the
  member's savings or guarantees dropped since approval, the payout is
  refused rather than paid against stale figures.

> ℹ️ The person who **created** the application cannot post its payout —
> a second person must do it. This is deliberate.

## Repayments

Open **Loan book** in the sidebar and select the loan.

![SCREENSHOT: The loan detail panel with schedule and repayment action](./images/PLACEHOLDER-loan-detail.png)
<!-- TODO(screenshot): capture the loan detail drawer showing balances, the schedule table and the record-repayment action, signed in as a Teller or Accountant -->

1. The panel shows the loan's balances, the instalment schedule, and an
   **early settlement quote** — what it costs to clear the loan today
   (future interest is waived; only what is already due is collected).
2. Choose the repayment action, enter the amount, channel and receipt
   reference, and confirm through the typed confirmation.

**What happens next**

Every repayment is split in a fixed order: **penalties first, then
interest due, then the loan balance**. You cannot collect more than the
full payoff — if the member wants to clear the loan, use the settlement
quote amount.

## Arrears and penalties

- An overnight check keeps every loan's days-late up to date. When a loan
  falls behind, the member's status becomes **Arrears** (money in only —
  see [Chapter 3](03-members.md#member-statuses--what-each-one-allows));
  when they catch up, it returns to Active automatically.
- If your SACCO has configured a late penalty, it accrues **daily** after
  the configured grace days, at the configured monthly rate spread over 30
  days. Penalties are collected first out of every repayment.
- Loans that stay behind long enough enter the recovery worklist — see
  [Chapter 8](08-corrections-recovery.md#the-recovery-worklist).
