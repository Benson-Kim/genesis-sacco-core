# 7. Member exit

When a member leaves the SACCO, everything is settled in one careful
workflow: their savings and shares are paid out, any loan is cleared from
the proceeds, and the membership ends. This chapter walks through it.

Open **Member exit** in the sidebar (under Governance).

![SCREENSHOT: The member exit register](./images/PLACEHOLDER-exits-register.png)
<!-- TODO(screenshot): capture the Member exit screen with exits in requested, approved and settled states, signed in as a Branch Manager -->

## The workflow at a glance

> **Requested → Approved (by committee) → Settled**

A request can be **rejected** at either of the first two steps. Settled and
rejected are final. A rejected exit changes nothing — the member simply
stays a member.

## Requesting an exit

**What you need before you start**

- The member's member number. Active, arrears and dormant members may all
  request an exit.
- Loose ends tidied up (the system checks these and will tell you):
  - **Guarantees the member has given** must first be released or another
    guarantor substituted in ([Chapter 5](05-loans.md#guarantors-and-pledges)).
  - **Open loan applications** must first be decided or withdrawn.
  - **An unresolved written-off loan** blocks the exit until the amount is
    recovered ([Chapter 8](08-corrections-recovery.md)).
  - A member can only have **one open exit request** at a time.

An **active loan does not block the exit** — it is paid off out of the
member's own money as part of the settlement.

**Steps**

1. Select the request action and enter the member number. The panel shows
   the member's status and each of the checks above, so you can see
   exactly what (if anything) stands in the way.

   ![SCREENSHOT: The exit request panel showing the eligibility checks](./images/PLACEHOLDER-exit-request-eligibility.png)
   <!-- TODO(screenshot): capture the "Request member exit" drawer after member lookup, with the eligibility checklist visible -->

2. Submit. The system freezes a **settlement snapshot** — the exact
   figures at this moment:

   | Line | Meaning |
   |---|---|
   | Shares | The member's share capital, paid out at exit |
   | Deposits | The member's savings |
   | Loan balance | What is still owed, cleared from the proceeds |
   | Exit fee | The fee your SACCO configured |
   | **Net payable** | Shares + deposits, minus the loan payoff and fee — what the member actually receives |

> ℹ️ **If the numbers come out negative** — the member owes more than they
> hold — the request is refused. The SACCO never pays out a negative
> settlement and never seizes a guarantor's money automatically. The member
> must first reduce the loan (deposits are always welcome) until the
> settlement is at least zero.

## Approval

Exit requests are decided by **committee vote**, exactly like loan
applications: one vote per committee member, a quorum decides, rejection
wins an ambiguous count.

![SCREENSHOT: An exit open for committee voting](./images/PLACEHOLDER-exit-vote.png)
<!-- TODO(screenshot): capture the exit detail drawer in the requested state with the vote actions visible, signed in as a Credit Committee member -->

## Settlement — the final step

**What you need before you start**

- If the member is owed money, the channel it will be paid on (M-Pesa or
  bank).

**Steps**

1. Open the approved exit and choose the settle action.
2. Review the settlement lines one last time and confirm through the typed
   confirmation.

   ![SCREENSHOT: The settle confirmation with the settlement lines](./images/PLACEHOLDER-exit-settle.png)
   <!-- TODO(screenshot): capture the settle-exit dialog on an approved exit, settlement breakdown visible, before confirming -->

**What happens next**

- Everything happens in one instant: the shares and deposits are closed
  out, the loan (if any) is paid off from the proceeds, the exit fee is
  taken, and the net amount is paid out to the member.
- The member's status becomes **Exited** — final. An exited member cannot
  transact, and the record never reopens; if the person ever rejoins, they
  are registered as a new member.
- A **member exit statement** documenting the settlement can be produced
  from the exit record or from Reports
  ([Chapter 9](09-reports-exports.md)).

> ⚠️ If the member's balances changed between approval and settlement (for
> example a deposit landed, or a penalty accrued), the settlement is
> refused rather than paid on stale numbers. Re-check the figures; the
> workflow will pick up the fresh state.

## What happens to shares and deposits

- **Deposits** are paid out (after covering the loan and fee).
- **Shares** are paid out too — the exit is one of only two ways share
  capital ever leaves a member's account (the other is a share transfer,
  [Chapter 6](06-shares-dividends.md#share-transfers-proposer-and-approver)).
- If a dividend was declared before the member exited but distributed
  after, their entitlement is not lost — it is parked as "unclaimed
  dividends" for the accountant to pay out separately.
