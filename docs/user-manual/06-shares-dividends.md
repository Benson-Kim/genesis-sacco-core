# 6. Shares and dividends

This chapter explains the difference between share capital and savings,
how shares move between members, and how yearly dividends are declared and
paid.

## Share capital vs deposits — in plain words

Every member holds two kinds of money in the SACCO:

- **Deposits (savings).** The member's own spendable money. It can be
  withdrawn (as long as it is not pledged as a guarantee), it backs their
  borrowing power, and it earns **deposit interest** and the yearly
  **rebate**.
- **Share capital.** The member's ownership stake in the SACCO. It earns
  the yearly **dividend** — but it is **not withdrawable**. Share capital
  only ever leaves a member's account in two ways:
  1. when the member **exits** the SACCO (Chapter 7), or
  2. by a **share transfer** to another member (below).

> ⚠️ When a member asks to "withdraw their shares", explain that shares are
> not withdrawable. If they want the money out, the routes are a transfer
> to another member or a full exit.

Buying more shares is a **share top-up**, posted like any other transaction
(see [Chapter 4](04-transactions.md)). Members who are active or in arrears
may top up shares; dormant members must first reactivate with a deposit.

## Share transfers (proposer and approver)

A share transfer moves share capital from one member to another. Because it
moves ownership, it uses a **two-person rule**, called *maker–checker*: one
person **proposes** the transfer, and a **different** person reviews and
**approves** it. No one can approve their own proposal — the system refuses
it. From here on this manual just says *proposer* and *approver*.

Open **Share transfers** in the sidebar (under Governance).

![SCREENSHOT: The share transfers register](./images/PLACEHOLDER-share-transfers-register.png)
<!-- TODO(screenshot): capture the Share transfers screen with one pending and one completed transfer, signed in as a Branch Manager -->

**What you need before you start**

- Both member numbers. **Both members must be active** — a transfer to or
  from a member in arrears, dormancy or exit is refused.
- The amount, which cannot exceed the giving member's share balance.

**Steps — proposing**

1. Select **Request transfer…**.
2. Enter the giving member, the receiving member, and the amount.
3. Confirm. The transfer is now **pending** — nothing has moved yet.

**Steps — approving (a different person)**

1. Open the pending transfer from the register.
2. Review the details, then choose **Approve share transfer** — or reject
   it with a written reason.

   ![SCREENSHOT: The transfer detail panel with the approve action](./images/PLACEHOLDER-share-transfer-approve.png)
   <!-- TODO(screenshot): capture the transfer detail drawer of a pending transfer, approve/reject actions visible, signed in as a different user than the proposer -->

**What happens next**

On approval, the shares move in one instant: the giving member's share
balance drops, the receiving member's rises, and both movements appear in
each member's history. A rejected transfer moves nothing and keeps the
reason on record.

> ℹ️ Auditors can see the whole transfer trail but can never propose or
> approve one — reviewing and doing are kept separate.

## Dividends and rebates

Once a year, after the financial year closes, the SACCO can declare:

- a **dividend** — a percentage return on each member's share capital, and
- a **rebate** — a percentage return on each member's deposits.

Both percentages come from your SACCO's settings, and both are computed on
each member's **average balance through the whole year** — money that was
in for half the year earns half as much. Members who are dormant remain
shareholders and are included.

Open **Dividends** in the sidebar (under Governance).

![SCREENSHOT: The dividends screen with a declaration listed](./images/PLACEHOLDER-dividends-screen.png)
<!-- TODO(screenshot): capture the Dividends screen with one declaration in the register, signed in as an Accountant -->

### Declaring

1. Select **Declare dividend**. The panel shows which financial year is
   being declared (always the most recently completed one — you cannot
   pick or backdate a year) and the configured rates.
2. Confirm. The declaration is created with the computed totals: eligible
   members, the share and deposit bases, and the dividend and rebate
   amounts.

   ![SCREENSHOT: A declaration with its computed totals](./images/PLACEHOLDER-dividend-declaration.png)
   <!-- TODO(screenshot): capture the declaration detail drawer showing financial year, rates, bases and totals -->

### Committee approval

A declaration must be approved by committee vote before anything is paid —
the same voting rules as loan applications: one vote per committee member,
a quorum decides, and rejection wins an ambiguous count.

### Distributing

Once approved, choose **Distribute**. Every eligible member is credited in
one run:

- By default the **dividend is added to the member's share capital** (so it
  grows next year's dividend too) and the **rebate is added to their
  deposits**.
- A member can instead have a stored payout preference (for example
  everything to deposits). Preferences that need mobile-money or bank
  payout are recorded but not yet paid externally — those members are
  credited the default way for now, with the preference noted on the
  record.
- A member who **exited** between declaration and distribution is not
  skipped silently: their entitlement is parked as a clearly visible
  "unclaimed dividends" amount for the accountant to resolve.

> ⚠️ Distribution moves money on every eligible member's account in one
> action. Check the declaration's totals before you confirm.
