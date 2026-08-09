# GENESIS PRESTIGE — OPS RUNBOOK

Operator procedures for surfaces that deliberately have **no console UI**.
Every entry cites the as-built handler actually read; nothing here is
speculative. Follows the evidence conventions of `docs/DSA_HARDENING.md`
and the STATUS register in `docs/BUILD_PROMPTS.md`.

---

## 1. One-off migration-era backfill jobs (P13.17a/b — issue #31 ledger (j).3)

**Maintainer decision 2026-08-07 (recorded on #31): these endpoints get NO
console surface — runbook-only.** They are one-off, migration-era backfills;
a permanent admin-screen affordance would invite routine re-runs of tools
whose purpose expires once the historical gap is filled. Disposition of
ledger item (j).3: retired, delivered as this runbook section.

Both endpoints live in `backend/src/genesis/api/accounting_periods.py`
(`jobs_router`) and are batched through the shared batch runner
(`application/batch_runner.py`): one unit of work per short transaction, so
a crash mid-run never holds locks and a re-run resumes where the claim keys
left off.

### 1.1 `POST /jobs/portfolio-snapshots` (P13.17a / DSA-1)

* **Purpose:** backfill month-end portfolio snapshots for months that
  elapsed before migration 0027 introduced the snapshot table. Snapshots
  freeze the month-end figures the NPL-trend export serves to auditors.
  One-off: once every historical month is snapshotted, subsequent runs are
  lock-free no-ops (anti-join on the claim key).
* **Auth / permission:** JWT bearer of a user holding
  `transactions × approve` (`ApproveCtx` — the close-period authority:
  System Admin, Branch Manager per the P4 matrix). Posting roles are
  deliberately excluded.
* **Request body:** `{}` — deliberately empty (`extra="forbid"`); the month
  worklist, cutoffs and batching are ALL server-resolved. Any smuggled
  field is a 422.
* **Expected response** (`200`, `SnapshotBackfillOut`):

  ```json
  { "months_considered": 18, "written": 18, "batches": 2 }
  ```

  A completed re-run returns `written: 0` — that is the proof of
  idempotency by side-effect counts, not an error.

### 1.2 `POST /jobs/period-rollups` (P13.17b / DSA-2/5)

* **Purpose:** backfill account/member rollups for accounting periods that
  were closed before migration 0028 wrote rollups at close time. One-off:
  the worklist is the server's own closed-but-unrolled period set; once it
  is empty every re-run scans zero rows and writes nothing.
* **Auth / permission:** identical to 1.1 — `transactions × approve`
  (`ApproveCtx`); rollups ARE the closed-period figures the trial balance
  serves, so the posting roles must not own them.
* **Request body:** `{}` — deliberately empty (`extra="forbid"`); no
  caller-supplied period identifiers exist.
* **Expected response** (`200`, `RollupBackfillOut`):

  ```json
  { "periods_rolled": 7, "accounts_written": 84, "members_written": 412, "batches": 1 }
  ```

### 1.3 Idempotency-Key custody (both jobs)

The house `Idempotency-Key` middleware (gate 1.4,
`backend/src/genesis/api/idempotency.py`) applies to these POSTs like every
mutation. Custody rules follow the dormancy-console precedent (one key per
user intent, held for the life of that intent's retries):

1. **Mint one UUIDv4 per backfill intent** and record it in the change
   ticket *before* the first request:

   ```shell
   KEY=$(uuidgen)   # record this in the ticket
   curl -X POST "$API/jobs/portfolio-snapshots" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Idempotency-Key: $KEY" \
     -H "Content-Type: application/json" \
     -d '{}'
   ```

2. **Retries of the same intent reuse the same key.** A stored response
   replays verbatim; a concurrent duplicate gets 409 while the original is
   in flight; a 5xx releases the claim so the same key can retry.
3. **A new intent gets a new key.** Because the jobs are no-op-on-complete,
   an accidental fresh-key re-run writes nothing — but the ticket trail
   should still show one key per intended run.
4. Replay scope is `(tenant, actor, method, path, body)` — a different
   operator presenting the same key can never read the first caller's
   stored response (review R4). Keys expire after
   `Settings.idempotency_retention_hours` (default 24h).

### 1.4 Verification after a run

* Response counters are the primary evidence (side-effect counts).
* Every batch writes in-transaction `audit_log` rows (gate 1.5) — filter
  `GET /audit-log` by the acting user around the run window.
* The NPL-trend export (1.1) and trial balance (1.2) are the consuming
  read models; spot-check one historical month/period each after the
  backfill.
