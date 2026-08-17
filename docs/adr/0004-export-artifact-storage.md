# ADR-0004: Export artifact storage — bounded bytea retained, object store deferred

- Status: Accepted (decision: KEEP bytea; object-store move DEFERRED
  with named revisit triggers)
- Date: 2026-08-02
- Deciders: P13.17(d) implementation (Developer + Solutions Architect
  roles), per docs/DSA_HARDENING.md DSA-4 ("needs an ADR per
  MASTER_PROMPT §6 — flagged, not assumed")

## Context

P13 stores each export's rendered CSV and PDF as `bytea` columns on
`export_artifacts` (migration 0013), downloaded through the app with
unguessable tokens, requester-only access and per-download audit rows
(P13 blockers e/f). DSA-4 flags two costs: (1) worker memory during
rendering, and (2) artifact bytes inflating the main database (TOAST
churn, backup size) with downloads streaming through the app.

Cost (1) is fixed in this MR without any storage change: incremental
rendering (`PdfBuilder`; `ExportRun` carries no rows) removes the
third in-memory copy of the row set. This ADR decides cost (2).

What bounds the liability today, measured against the shipped
configuration (`settings.py`):

- `export_row_cap = 10_000` rows per artifact — a hard server-side
  cap, never caller-supplied (P13 blocker a);
- `export_artifact_ttl_hours = 24` — expired artifacts refuse to
  download; rows are short-lived working data, not an archive;
- artifacts hold REPORT output (already PII-gated by the frozen
  column allow-list), not member document binaries.

Worst-case live artifact volume ≈ tenants × pending/recent exports ×
(CSV + PDF of ≤10k text rows) — single-digit MB per artifact, hours of
lifetime. This is not the unbounded-growth class of DSA-3/DSA-6.

## Decision

1. **Keep `bytea` artifacts** with the existing cap + TTL bounds. The
   DB row remains the single fetch path behind RLS, the tenant
   predicate, requester-only token resolution and the in-transaction
   download audit — the exfiltration-control chain P13 blocker f
   built, with no second storage system to secure.
2. **Do NOT move to object storage now.** The move is deferred, not
   rejected on the merits: at the current bounds the operational cost
   of a new backend exceeds the TOAST/backup relief it buys.
3. **Named revisit triggers** (any one reopens this ADR):
   - `export_row_cap` needs to rise above 10k for a real tenant, or
     the TTL needs to exceed 72h (archival semantics);
   - P21 load tests show export-related DB bloat, backup duration or
     download latency breaching budget;
   - member-document binary upload (ADR-0003, also deferred) lands an
     object-store adapter — exports then reuse that adapter and its
     controls instead of pioneering one.

## Alternatives considered

- **S3/GCS-compatible object store behind an infrastructure adapter
  (the DSA-4 "replacement" sketch)** — rejected FOR NOW: adds a new
  supply-chain and ops surface (client dependency, credentials,
  lifecycle rules, per-tenant key prefixes, signed-URL or
  proxy-download design) that must re-implement the audit-on-download
  and requester-only guarantees the DB row gives for free today; the
  bounded cap × TTL working set does not yet justify it. Deferral
  cost is small and the read path is already behind
  `download_artifact`, so the adapter seam exists.
- **Larger bytea with external TOAST tuning / separate tablespace** —
  rejected: tuning around the symptom; keeps backup inflation while
  adding ops complexity nobody asked for at this volume.
- **Dropping the PDF artifact to halve the volume** — rejected: the
  prototype's export surface promises both formats; product scope is
  not a storage optimisation.

## Consequences

- Positive: zero new dependencies or trust boundaries (STRIDE/DFD
  diagrams unchanged); the P13 download-control chain stays intact;
  rollback of P13.17(d) is code-only (no data migration).
- Negative (accepted, bounded): artifact bytes ride the main DB and
  its backups for their ≤24h lifetime; downloads stream through the
  app process. Both are capped by `export_row_cap` × TTL and revisit
  triggers above are named.
- Migration path if reopened: add the adapter interface in
  `infrastructure/`, dual-write behind a flag, flip reads, then a
  contract-phase migration drops the bytea columns (expand →
  migrate → contract, §3).
