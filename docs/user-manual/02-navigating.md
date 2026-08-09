# 2. Finding your way around

This chapter explains the layout of the console and the parts you will use
on every screen: the menu, tables, filters, and detail panels.

## The layout

The console has three parts:

- **The header** (top) — the product name and the **Sign out** button.
- **The sidebar** (left) — the menu. It is grouped into sections:
  - **Operations** — Dashboard, Members, Applications, Loan book,
    Guarantors, Transactions.
  - **Governance** — Credit committee, Member exit, Dormancy,
    Share transfers, Dividends, Corrections, Accounting periods, Recovery.
  - **Insights** — Reports.
  - **Administration** (pinned at the bottom) — Settings, Branches,
    Access control, Audit log.
- **The work area** (centre) — the screen you are working on.

![SCREENSHOT: The full console layout with the sidebar sections labelled](./images/PLACEHOLDER-console-layout.png)
<!-- TODO(screenshot): capture the /dashboard screen signed in as a System Admin so every sidebar entry is visible; annotate header, sidebar, work area -->

> ℹ️ You only see the menu entries your role may use. If a colleague sees
> "Corrections" and you do not, that is your permissions, not a fault.

## Tables and lists

Most screens are a table of records — members, transactions, loans, and so
on.

![SCREENSHOT: A typical table with paging controls, filters and search visible](./images/PLACEHOLDER-table-anatomy.png)
<!-- TODO(screenshot): capture the Members register with at least two pages of data, signed in as a Branch Manager -->

- **Paging.** Tables show one page at a time. Use the paging controls under
  the table to move forward through the pages. Newest records come first.
- **Filters.** Buttons and drop-downs above the table narrow the list — for
  example by status, type, or date. Filters combine: a status filter plus a
  date range shows only rows matching both.
- **Search.** Where a search box exists, it finds matching records by the
  fields named next to the box (for example a name, a member number, or a
  receipt reference).
- **Status pills.** Coloured labels in a row show the record's current
  state (for example *Active*, *Committee*, *Settled*).

## Detail panels ("drawers")

Selecting a row opens a panel from the side of the screen with the full
details of that record. We call this panel a **drawer**.

![SCREENSHOT: A detail drawer open over a table](./images/PLACEHOLDER-detail-drawer.png)
<!-- TODO(screenshot): capture the member detail drawer open from the Members register, any active member, signed in as a Branch Manager -->

- Drawers open **read-only**. You can review everything without any risk of
  changing it.
- If your role may change the record, the drawer shows an **Edit** button.
  Selecting it turns the fields editable; you then **Save** or cancel.
- Actions that move money or change a record's state have their own clearly
  labelled buttons and always ask you to confirm first.

> ⚠️ For actions that move money, the confirmation asks you to type
> something specific (for example the member's number) before the button
> unlocks. This is deliberate — it makes an accidental confirmation almost
> impossible. Read the confirmation text; it states exactly what will
> happen.

## How records are labelled

- Every member has a unique **member number** like `GP-0007`. Wherever a
  member appears, you will usually see the **name and number together** —
  for example "Jane Wanjiku · GP-0007" — so that two members with the same
  name can never be confused.
- Every money posting has a unique **reference** whose first letters tell
  you what it is: deposits start with `MP-` (M-Pesa) or `BK-` (bank),
  withdrawals with `WD-`, loan payouts with `LN-`, repayments with `RP-`,
  share top-ups with `SH-`, and so on.

## If something goes wrong on a screen

- A **banner** at the top of the screen or panel explains the problem in
  one sentence.
- The message "someone else changed this record" means a colleague saved a
  change while you had the record open. The screen refreshes to show the
  latest version; simply redo your change on the fresh copy.
- See [Chapter 11 — Troubleshooting](11-troubleshooting.md) for the common
  messages and what to do about each.
