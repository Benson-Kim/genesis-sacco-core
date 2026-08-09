<!--
  P-DIAG user flow — TELLER MONEY-IN (deposit / loan repayment)
  Authored against main @ 8f46aa54250ff1a066af423924f3eb54a9c72fb7
  by the P-DIAG drift MR. Business-facing user-flow diagram
  (decision-diamond style, P-DIAG audience rule): every outcome the
  teller can see is drawn, incl. the refusals; code citations live in
  the Source-of-truth footer.
  Drift rule: v1.2 rule 11 — any MR that changes a refusal outcome or
  the allocation/reactivation behaviour MUST update this file.
-->

# User flow — teller money-in: deposit & loan repayment

**Audience: business (tellers, branch managers, auditors).**

## The business rules this depicts

Money in is welcome — but only through the front door, with every
refusal explicit. A deposit wakes a dormant member automatically. A
repayment always pays penalties first, then interest, then principal;
paying more than the full payoff is refused (collect at most the
settlement quote); and a written-off loan takes NO repayments — cash
against a written-off loan goes through the recovery receipt instead
(its own flow). Submitting the same transaction twice (double click,
network retry) produces exactly one effect and the same receipt.

```mermaid
flowchart TD
    START(["Teller: record money in"]) --> KIND{"what is it?"}

    KIND -->|"savings deposit /<br/>share top-up"| DUP1{"same request<br/>already submitted?"}
    DUP1 -->|"yes"| REPLAY1["same receipt returned —<br/>money counted ONCE"]
    DUP1 -->|"no"| AMT1{"amount positive?"}
    AMT1 -->|"no"| R1["REFUSED — invalid amount"]
    AMT1 -->|"yes"| MEM1{"member's standing?"}
    MEM1 -->|"exited"| R2["REFUSED — exited members<br/>cannot transact"]
    MEM1 -->|"dormant (deposit)"| WAKE["deposit accepted AND the<br/>member wakes to active —<br/>same step, one record"]
    MEM1 -->|"dormant (share top-up)"| R3["REFUSED — top-ups need<br/>an active member"]
    MEM1 -->|"active / in arrears"| PER1{"accounting period open?"}
    WAKE --> PER1
    PER1 -->|"closed"| R4["REFUSED — period closed;<br/>see the accountant"]
    PER1 -->|"open"| OK1["POSTED: balance up, permanent<br/>ledger entry, exact figures in the<br/>audit trail, receipt printed"]

    KIND -->|"loan repayment"| DUP2{"same request<br/>already submitted?"}
    DUP2 -->|"yes"| REPLAY2["same receipt returned —<br/>money counted ONCE"]
    DUP2 -->|"no"| AMT2{"amount positive?"}
    AMT2 -->|"no"| R5["REFUSED — invalid amount"]
    AMT2 -->|"yes"| LOAN{"loan status?"}
    LOAN -->|"closed"| R6["REFUSED — nothing to repay"]
    LOAN -->|"written off"| R7["REFUSED — use the RECOVERY<br/>RECEIPT: cash against a written-off<br/>loan is claim recovery, never<br/>loan servicing"]
    LOAN -->|"active"| OVER{"more than the<br/>full payoff?"}
    OVER -->|"yes"| R8["REFUSED — collect at most<br/>the settlement quote"]
    OVER -->|"no"| ALLOC["allocated in fixed order:<br/>penalties → interest → principal<br/>(the member cannot choose)"]
    ALLOC --> PER2{"accounting period open?"}
    PER2 -->|"closed"| R9["REFUSED — period closed"]
    PER2 -->|"open"| PAID{"loan fully paid off?"}
    PAID -->|"no"| OK2["POSTED: balance down,<br/>schedule updated, receipt printed"]
    PAID -->|"yes"| OK3["POSTED + loan CLOSED +<br/>guarantors released +<br/>closure notice queued"]
```

## Source of truth (code citations, valid at `8f46aa5`)

| Flow step | Implementation |
|---|---|
| Duplicate-submission shield | `api/idempotency.py:IdempotencyMiddleware` — atomic claim per (tenant, actor, route+body); replay returns the stored response; expiry per 0029 |
| Deposit / top-up service | `application/transactions.py:record_deposit` / `record_share_topup` (routes `api/transactions.py`, `RequirePermission(TRANSACTIONS, CREATE)`, `extra="forbid"`) |
| Amount guards | `InvalidInputError` on non-positive amounts (`record_deposit` / `record_share_topup` / `application/loans.py:record_repayment`) |
| Member-standing gate | `application/transactions.py:_require_member` → `domain/members.py:member_may` (code-owned capability map: deposits allowed for active/arrears/dormant; top-ups active/arrears only; exited always refused) |
| Dormant wake-up | `application/members.py:reactivate_dormant_member` — inside the SAME deposit transaction (P13.13) |
| Period-closed refusal | `application/accounting_periods.py:assert_open_period`, called by every posting via `application/ledger.py:_post` (locks: lock-order.md E15 → E16) |
| Repayment allocation + overpayment refusal | `domain/lending.py:allocate_repayment` (penalties → interest → principal; amount > payoff rejected) via `application/loans.py:record_repayment` (loan row held: lock-order.md §3 repayment row) |
| Written-off refusal | `record_repayment` status guard (`is not LoanStatus.ACTIVE`); the recovery path is `application/corrections.py:record_recovery_receipt` — [`sequence-recovery-receipt.md`](sequence-recovery-receipt.md) |
| Closure + guarantor release | `application/loans.py:_close_loan` → `application/guarantees.py:release_guarantees_for_loan` (lock-order.md E7) |
| Receipts / audit / notices | balanced postings via `application/ledger.py` (append-only, 0004/0014 triggers); `application/audit.py:record_audit` (exact figures); `application/outbox.py:enqueue_event` |
