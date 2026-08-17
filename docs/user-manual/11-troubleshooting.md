# 11. Troubleshooting

The system prefers a clear "no" over a silent mistake, so you will meet
refusal messages in normal work. This chapter explains the common ones in
plain words: what each means and what to do.

A general point first: refusal messages are deliberately short and never
show balances or figures. That is a privacy feature, not missing
information — the exact figures are always in the audit trail for the
staff entitled to see them ([Chapter 10](10-administration.md#the-audit-trail)).

## Signing in

| What you see | What it means | What to do |
|---|---|---|
| No code arrives | Delivery lag, or the identifier is not the one registered for you. The system never says whether an address is known. | Wait a minute, check your typing, request a new code. Still nothing — ask your administrator to check your account details. See [Chapter 1](01-getting-started.md#if-the-code-does-not-arrive). |
| "Incorrect or expired code" | Wrong digits, a code older than 5 minutes, an already-used code, or an older code after a newer one was requested. | Request a fresh code and use the newest one. |
| Code refused after several tries | 5 wrong attempts lock that code. | Request a fresh code. |
| "Too many requests" | You asked for codes too quickly. | Wait a minute and try again. |
| You are suddenly signed out | Your session expired, or an administrator suspended the account. | Sign in again; if you cannot, talk to your administrator. |

## Money postings

| What you see | What it means | What to do |
|---|---|---|
| **Refused withdrawal** — "insufficient available funds" | The amount is more than the member can withdraw. Note: "available" is the balance **minus anything pledged as a guarantee** — the account can hold more than is withdrawable. | Check the member's guarantees ([Chapter 5](05-loans.md#guarantors-and-pledges)). Release/substitute a pledge, or withdraw a smaller amount. |
| **Duplicate reference** | The M-Pesa code or bank reference was already posted on that channel. | Almost always the receipt is already captured — search the register for the reference ([Chapter 4](04-transactions.md#searching-and-filtering-the-register)). Only if it truly is a different receipt, re-check the code you typed. |
| **Invalid reference format** | The reference does not match the required shape — M-Pesa: exactly 10 letters/digits; bank: 2–40 characters. | Re-read the receipt and type the reference exactly. |
| Posting refused for a member | The member's status does not allow that action — e.g. withdrawals need an active member; exited members cannot transact. | Check the status rules in [Chapter 3](03-members.md#member-statuses--what-each-one-allows). |
| Date refused | Dates in the future are never accepted for postings and reports. | Use today or a past date. |

## Permissions and approvals

| What you see | What it means | What to do |
|---|---|---|
| **Permission denied** | Your role does not include that action. | If you should have it, ask your administrator ([Chapter 10](10-administration.md#the-permission-matrix)). |
| **Amount above your approval limit** | The approval matrix caps what your role may approve, and this amount is above your rung. | Hand it to a colleague whose role covers the amount. Above the top rung, the decision is made outside the system. |
| Your vote is refused on an application | You recommended it to committee — the recommender never votes. | Another committee member must vote. |
| You cannot approve a correction or transfer | You proposed it (no one approves their own work), or you hold the Auditor role (auditors never approve). | A different, non-auditor colleague approves. |
| **Quorum not reached** | Not enough committee votes on one side yet — the item stays undecided. | Nothing is wrong; more committee members need to vote. |
| "One vote per committee member" | You already voted on this item. | Nothing to do — your vote is counted. |

## Records and screens

| What you see | What it means | What to do |
|---|---|---|
| "Someone else changed this record" | A colleague saved a change while you had it open. Your change was **not** applied. | The screen reloads the latest version — redo your change on the fresh copy. |
| "Not found" | The record does not exist — often a mistyped member number. | Check the number and try again. |
| A member/exit/transfer action is "blocked" | A workflow rule stands in the way — e.g. an exit blocked by live guarantees or an unresolved write-off. The screen lists the blockers. | Clear the listed blockers first ([Chapter 7](07-member-exit.md#requesting-an-exit)). |
| An unexpected error with a reference code | Something went wrong on the server. The code identifies your exact request in the logs. | Try again; if it persists, give the reference code to your administrator or support contact. |
| An export never finishes or is cut short | Very large exports stop at a fixed limit and say so. | Narrow the date range and run again ([Chapter 9](09-reports-exports.md#things-worth-knowing)). |

## Who to contact

1. **Your branch manager or administrator** — permissions, account
   details, approval limits, and anything about your SACCO's settings.
2. **Your SACCO's designated support contact** — errors with a reference
   code, suspected faults, and anything this manual does not cover. Give
   them: what you were doing, the exact message, the time, and the
   reference code if one was shown.

> ⚠️ Never work around a refusal by inventing data — for example, never
> post a made-up receipt reference to get past a duplicate message. Every
> action is permanently recorded under your name.
