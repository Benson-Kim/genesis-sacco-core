# ADR-0003: Member KYC document storage — metadata now, object store deferred

- Status: Accepted
- Date: 2026-08-01
- Deciders: P13.12 implementation (Developer + Security Analyst roles)

## Context
P13.12 persists the prototype's per-type member document checklist
(GAP_ANALYSIS §2.3). The prompt requires document *binary content* to
live behind an infrastructure storage adapter, and states the
object-store choice needs an ADR (MASTER_PROMPT §6); until that choice
is made, "metadata-only with upload deferred is acceptable and
recorded". Constraints that bind the eventual choice:

- Tenant isolation (gate 1.6 / ADR-0002): stored objects must be
  keyed and access-checked per tenant; a leaked object key must not be
  fetchable across tenants.
- Auditability (P13 blocker f): every content access must write an
  audit row, exactly as the metadata reads shipped in P13.12 already do.
- DPA-2019: KYC images are sensitive personal data — encryption at
  rest and deletion/retention controls are required.
- No new dependency without an ADR (§6); no blocking I/O inside
  transactions holding row locks (gate 1.3).

## Decision
1. **Ship metadata-only now.** `member_documents` (migration 0018)
   carries type, status, expiry, versioning and audit; there is no
   upload/download endpoint and no binary column. This unblocks the
   registration workflow (checklist tracking) without committing to a
   storage backend.
2. **Defer the object-store choice** to a follow-up ADR that
   supersedes this one. The storage port will live in
   `genesis/infrastructure` behind an interface (the providers/export
   pattern), added TOGETHER with its first real adapter — no dead
   interface is merged ahead of need ("no TODOs in merged code").
3. Candidates to evaluate then: S3-compatible object storage
   (MinIO/GCS/S3) with per-tenant prefixes + SSE, versus Postgres
   `bytea` (the exports-artifact precedent, acceptable for small
   bounded artifacts but wrong for unbounded KYC scans and backups).

## Alternatives considered
- Postgres `bytea` now (exports precedent) — rejected: KYC images are
  unbounded in size and count and would bloat the OLTP database,
  backups and RLS-scanned pages; exports artifacts are small,
  short-TTL and purged.
- Picking S3/MinIO now — rejected: adds a new deployment dependency
  and secret surface with no consuming client yet (Phase C/D mobile &
  web uploads); choosing under time pressure violates §6 diligence.

## Consequences
- Positive: registration checklist is fully functional and audited;
  no supply-chain or infrastructure commitment made prematurely.
- Negative: no binary upload until the follow-up ADR lands; clients
  track document state only. This deferral is recorded in the P13.12
  MR description and in `application/member_kyc.py`.
- Negative (review K4): per-read access auditing (checklist and
  profile reads) makes `audit_log` grow with browse/poll traffic, not
  just mutations. Accepted — DPA-2019 traceability of PII reads
  outweighs storage — with a recorded follow-up: an `audit_log`
  retention/partitioning policy plus per-(actor, member, day)
  aggregation for `*.access` rows, tracked in the P13.12 MR
  follow-ups list.
- Migration path: adding content is expand-only (new storage keys on
  `member_documents` or a side table + the storage adapter); nothing
  in 0018 needs rework.
