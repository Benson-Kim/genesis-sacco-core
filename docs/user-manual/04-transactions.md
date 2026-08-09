# 4. Deposits and withdrawals

This chapter shows you how to post money in and out of member accounts, why
receipt references matter, and how to search the transactions register.

Open **Transactions** in the sidebar.

![SCREENSHOT: The Transactions register with filters and search visible](./images/PLACEHOLDER-transactions-register.png)
<!-- TODO(screenshot): capture the Transactions register with several rows of mixed types, signed in as a Teller -->

## Posting a deposit, withdrawal or share top-up

**What you need before you start**

- The member's **member number**.
- The **amount**.
- The **channel** the money moved on: M-Pesa or bank.
- The **receipt reference** from that channel (see below — it is required).

**Steps**

1. On the Transactions screen, select the button to post a transaction. The
   posting panel opens.

   ![SCREENSHOT: The post-transaction panel, empty](./images/PLACEHOLDER-post-transaction-drawer.png)
   <!-- TODO(screenshot): capture the post-transaction drawer just opened, type "Deposit" selected, no member entered, signed in as a Teller -->

2. Choose the **Type**: Deposit, Withdrawal, or Share top-up.
3. Enter the **member number** and move to the next field. The panel looks
   the member up and shows "Member verified" with their name and number.
   Check it is the right person.

   ![SCREENSHOT: The post-transaction panel with a verified member shown](./images/PLACEHOLDER-post-transaction-member-verified.png)
   <!-- TODO(screenshot): capture the drawer after member lookup succeeds, showing the "Member verified: name · number" line -->

4. Enter the **amount** in KES.
5. Choose the **channel**: M-Pesa or bank.
6. Enter the **external reference** — the M-Pesa confirmation code or the
   bank slip reference.
7. Submit. A confirmation appears describing exactly what will be posted —
   the type, the amount, the channel, and the member. To unlock the confirm
   button, **type the member's number** into the confirmation box.

   ![SCREENSHOT: The typed confirmation before posting money](./images/PLACEHOLDER-post-transaction-confirm.png)
   <!-- TODO(screenshot): capture the confirmation dialog with the member-number confirmation box visible, before typing -->

8. Confirm. The panel shows the result with the new posting's reference
   (for example `MP-000123`).

**What happens next**

- The member's balance updates immediately and the posting appears at the
  top of the register.
- Every posting is recorded permanently. Mistakes are fixed later with a
  correcting entry (see [Chapter 8](08-corrections-recovery.md)) — never by
  deleting or editing.

> ⚠️ **Check before you confirm.** Postings move real money and cannot be
> edited afterwards. The typed confirmation is your last chance to catch a
> wrong member or a wrong amount.

## Why the receipt reference is required — and its exact shape

Money on M-Pesa or a bank account moves **outside** this system. The receipt
reference ties your posting to the real-world receipt so the books can be
reconciled — and so the **same receipt can never be posted twice**.

- **M-Pesa**: the confirmation code, exactly **10 letters and digits** (for
  example `SGH3KLM9QT`). Small letters are fine; the system stores it in
  capitals.
- **Bank**: the slip or transfer reference, **2 to 40 characters** — letters
  and digits, optionally with spaces, dashes or slashes in the middle.

If you post a reference that has already been used on the same channel, the
posting is refused with a duplicate message. That usually means the receipt
was already captured — search the register for the reference before trying
again.

## Rules the system enforces for you

- **Withdrawals never overdraw.** The withdrawable amount also excludes any
  savings the member has pledged as a guarantee for someone else's loan. If
  the amount is more than what is available, the posting is refused.
- **Member status matters.** Deposits are accepted from active, arrears and
  dormant members (a deposit instantly reactivates a dormant member).
  Withdrawals need a fully **active** member. Share top-ups are accepted
  from active and arrears members. See
  [Chapter 3](03-members.md#member-statuses--what-each-one-allows).
- **Share money is different.** A share top-up buys share capital, which is
  not withdrawable — see [Chapter 6](06-shares-dividends.md).

## Searching and filtering the register

- **Type filter** — show only deposits, withdrawals, repayments, and so on.
- **Direction filter** — money in vs money out, from the member's point of
  view.
- **Date presets** — one press for **Today**, the **last 7 days**, or the
  **last 30 days**; or pick your own from/to dates. Typing your own dates
  overrides any preset.
- **Search** — type a posting reference (or just its beginning, like
  `MP-`), an external receipt reference, a member number, or a member name.

![SCREENSHOT: The register filtered to withdrawals in the last 7 days](./images/PLACEHOLDER-transactions-filtered.png)
<!-- TODO(screenshot): capture the Transactions register with Direction=money out and the 7-day date preset pressed -->

Selecting a row opens the transaction's detail panel, including the
double-entry breakdown behind the posting.

## Exporting

Transaction data leaves the system through the **Reports** screen — for
example the *Member statement* or the *Disbursements & collections* report,
each with date filters. See [Chapter 9](09-reports-exports.md).

## Interest runs

If your role covers it, the Transactions area also offers the **interest
run** action, which posts the period's deposit interest for all qualifying
accounts in one go. The rate and rules come from your SACCO's settings —
the run never asks you for a rate.
