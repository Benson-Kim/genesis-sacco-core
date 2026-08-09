# 8. Corrections and recovery

Mistakes happen, fees need charging, and some loans go bad. This chapter
covers the **Corrections** screen (adjustments, fees, write-offs, recovery
receipts) and the **Recovery** worklist.

A principle first: **nothing in the books is ever edited or deleted.** A
correction posts a new, opposite entry that cancels the wrong one. Both
stay visible forever, which is what keeps the books trustworthy.

Open **Corrections** in the sidebar (under Governance).

![SCREENSHOT: The Corrections register](./images/PLACEHOLDER-corrections-register.png)
<!-- TODO(screenshot): capture the Corrections screen with a pending adjustment and a write-off in the register, signed in as an Accountant -->

## Requesting a correction (proposer and approver)

Repayment corrections use the two-person rule from
[Chapter 6](06-shares-dividends.md#share-transfers-proposer-and-approver):
one person **proposes**, a different person **approves**.

**Typical use:** a repayment was posted to the wrong loan, or with the
wrong amount.

**Steps — proposing (usually the accountant)**

1. Select the adjustment request action.
2. Identify the repayment to be corrected and give the reason.
3. Submit. The request is now pending — nothing has changed in the books
   yet.

**Steps — approving (a different person, usually a branch manager or
committee member)**

1. Open the pending adjustment from the register.
2. Review it and approve — or reject it with the reason.

   ![SCREENSHOT: A pending adjustment with the approve action](./images/PLACEHOLDER-adjustment-approve.png)
   <!-- TODO(screenshot): capture the adjustment detail drawer of a pending request, approve/reject visible, signed in as a Branch Manager -->

**What happens next**

Approval posts the cancelling entry. The loan's balances are restored to
what they should be — if the wrong repayment had closed the loan, the loan
reopens with the amount outstanding again. Larger adjustments also respect
approval limits ([Chapter 5](05-loans.md#approval-limits-by-role)).

> ℹ️ You can never approve your own request, and auditors can never approve
> anyone's — reviewing and doing are kept separate.

## Charging a fee

1. On the Corrections screen, choose the fee action.
2. Enter the member number and pick the **fee type** (for example the
   registration fee). **The amount is fixed by your SACCO's settings** —
   the screen shows it, and nobody can type a different figure.
3. Pick the channel, enter the receipt reference, and confirm.

Fees are money in: active, arrears and dormant members can all pay one.

## Writing off a loan

When a loan is judged unrecoverable, it can be written off. A write-off is
a big decision, so it takes a request **plus committee votes**, and the
amount must be within the approver's limit.

1. Request the write-off from the Corrections screen, with the reason.
2. Committee members vote, exactly as for loan applications.
3. On approval, the loan is written off.

**What a write-off means — and does not mean**

- The loan stops counting as an asset in the books, and its status becomes
  **written off** (final — it never becomes a normal loan again).
- **The member still owes the money.** The claim survives in full, the
  member cannot exit the SACCO while it is unresolved, and anything later
  recovered is recorded against it (below).

> ⚠️ A write-off is an accounting decision, not forgiveness. Tell members
> with a written-off loan that the debt still stands.

## Recording a recovery receipt

When money comes in against a written-off loan — from the member, an
auction, or a negotiated settlement:

1. On the Corrections screen, choose the recovery receipt action.
2. Enter the amount actually received, the channel and the receipt
   reference, and confirm.

The receipt reduces the outstanding claim; you can never record more than
what is still owed. When the claim reaches zero, the member is free to
exit again.

## The recovery worklist

Open **Recovery** in the sidebar (under Governance). This is the
collections team's queue of problem loans.

![SCREENSHOT: The recovery worklist](./images/PLACEHOLDER-recovery-worklist.png)
<!-- TODO(screenshot): capture the Recovery screen with cases in open and paused states, signed in as a Loan Officer -->

### Opening a case

A case can be opened for a loan that is **seriously overdue** (roughly
three months or more behind). The system reads the loan's arrears facts
itself when the case opens — the snapshot on the case is always the
system's, never typed in. One live case per loan.

### Working a case

- **Assign** the case to a recovery officer (auditors cannot be assigned).
- **Add notes** as the work progresses — calls made, promises received.
  Notes are permanent; you can add but never edit or remove one.
- **Change the case's posture** when the situation changes:

| Posture | Meaning |
|---|---|
| **Open** | Being actively worked. |
| **Irrecoverable — pending write-off** | The officer has concluded recovery is impossible; awaiting the committee's write-off decision. |
| **Disputed** | The member contests the arrears; the case pauses. |

A paused case returns to Open before taking a different posture, so every
change of direction is a visible step.

### How cases close

Cases close in three ways — the first two happen **automatically**:

- **Cured** — the member caught up; the overnight check closes the case.
- **Written off** — the committee's write-off landed; the overnight check
  closes the case.
- **Restructured** — staff close the case because the loan was
  restructured and the case no longer applies.

Staff can never simply declare a loan "cured" — only the money facts can.
A closed case never reopens; if the same loan defaults again later, a new
case is opened, and the old case's history stays intact. One final outcome
note can be added to a closed case.
