# 9. Reports and exports

This chapter lists the reports the system can produce and shows you how to
run one.

Open **Reports** in the sidebar.

![SCREENSHOT: The Reports screen with the report cards](./images/PLACEHOLDER-reports-screen.png)
<!-- TODO(screenshot): capture the Reports screen showing the report cards and the recent-exports list, signed in as an Accountant -->

## The available reports

| Report | What it shows |
|---|---|
| **Member statement** | One member's full money history — deposits, withdrawals, repayments, interest, shares. |
| **Membership register** | The member list with statuses. |
| **Trial balance** | The accountant's check that the books balance. |
| **Income statement** | Income and expenses over a period. |
| **Loan book — classification & provisions** | Every loan with how overdue it is and the provision held against it. |
| **Portfolio at risk — aging** | Overdue loans grouped by how late they are. |
| **NPL trend (monthly)** | How the problem-loan share of the portfolio has moved month by month. |
| **Disbursements & collections** | Money lent out vs money collected over a period. |
| **Dividend & rebate schedule** | Every member's entitlement under a dividend declaration. |
| **Member exit statement** | The settlement document for one member's exit. |
| **SASRA return (skeleton)** | The regulator return, in outline form. |

## Running an export

Reports are produced as **exports**: you request one, the system prepares
it in the background, and you download the finished file (CSV — it opens
in any spreadsheet).

**What you need before you start**

- Which report you want, and its scope — for a member statement, the
  member; for period reports, the date range.

**Steps**

1. On the Reports screen, choose the report's **request** action.
2. Fill in the filters the report offers. **Only the filters that make
   sense for that report are shown** — a trial balance takes none, a
   member statement asks for the member and an optional date range,
   period reports ask for dates. Required filters are marked.

   ![SCREENSHOT: The export request panel for a member statement](./images/PLACEHOLDER-export-request.png)
   <!-- TODO(screenshot): capture the export request drawer for "Member statement" with the member filter filled in -->

3. Submit. The export appears in the list with its status.
4. When it is ready, open it. You can preview the contents on screen and
   **download** the file.

   ![SCREENSHOT: A finished export ready for download](./images/PLACEHOLDER-export-ready.png)
   <!-- TODO(screenshot): capture the export view drawer of a completed export with the download control visible -->

**What happens next**

- The filters you chose are stamped on the export record, so anyone
  looking at it later can see exactly what scope it covers.
- Finished files are kept for a limited time — download what you need to
  keep.

## Things worth knowing

- **Size limits.** Very large exports are cut off at a fixed row limit,
  and the export clearly says so when that happens. Narrow the date range
  and run again for the rest.
- **Scope is fixed per report.** You choose *what* (the report) and *which
  slice* (member, dates). Formats and limits are set by the system — there
  is nothing else to configure, and nothing to get wrong.
- **Asking twice creates two exports.** Each request is its own job with
  its own file; requesting the same report again gives you a fresh export
  reflecting the books right now.
- The exit statement for a specific exit can also be reached from the exit
  record itself ([Chapter 7](07-member-exit.md)).
