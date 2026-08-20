# Operations

CI/CD, migrations, environment flags and development constraints. The
pipeline definition is `.gitlab-ci.yml`; CI is the **sole arbiter** — local
runs are conveniences, never evidence.

## 1. CI pipeline

Stages: `lint → test → security → build → migrate-check`. Jobs are gated
with `rules:exists` so they activate as each codebase lands.

| Job | Stage | Arbitrates |
|---|---|---|
| `backend:lint` | lint | `ruff check` + `ruff format --check` + `mypy --strict` + `lint-imports` (layer boundaries). Zero warnings. |
| `web:lint` | lint | ESLint + `tsc --noEmit` (TypeScript strict). |
| `docs:diagrams` | lint | Renders every `.mmd` file and every ```` ```mermaid ```` block under `docs/diagrams/` with mermaid-cli — an unrenderable diagram is a red pipeline. Runs only when `docs/diagrams/**` changes. **Syntax gate only.** |
| `docs:spot-check` | lint | The **semantic** diagram gates: `docs/diagrams/c4-spot-check.py` and `docs/diagrams/erd-spot-check.py` verify diagram claims against real module paths, router wiring and the migration chain. On merge requests it runs when the diagram files, the router wiring or the migration chain change; on the default branch it runs **unconditionally** (a squash merge lands file combinations no MR pipeline tested together). |
| `backend:lock-drift` | lint | Re-compiles `backend/requirements.txt` (the hash-pinned lock) from `backend/pyproject.toml` and fails on any diff — editing the ranges without regenerating the lock, or hand-editing the lock, is a red pipeline. On failure the regenerated lock is attached as an artifact (the `web:spec-drift` precedent — take the artifact, never hand-edit). |
| `backend:test` | test | pytest against a real PostgreSQL 16 service with migrations applied and **RLS actually enforced**: the job creates a non-superuser, non-BYPASSRLS application role and runs the suite through it. Coverage gate ≥ 85%; captured EXPLAIN plans are printed in `after_script` so MR authors can copy evidence from the job trace. |
| `web:test` | test | Jest suites, including per-module network wire tests. |
| `web:e2e` | test | Playwright against the real production build (`next start` — middleware/CSP live), API mocked at the browser network boundary; request counts/bodies are the assertion surface for double-submit and 409 single-attempt proofs. |
| `web:spec-drift` | test | Regenerates the OpenAPI document from the backend and diffs it byte-for-byte against the committed snapshot. |
| `web:client-drift` | test | Regenerates the client from the snapshot, diffs against the committed generated client, and proves its own falsifiability on every run (a deliberately staled client must fail the check). |
| SAST / Secret Detection / Dependency Scanning | security | GitLab `latest` template variants (they include merge-request rules). Critical findings block merge. Dependency Scanning reads `backend/requirements.txt` — since issue #5 that file **is** the hash-pinned lock, so scan coverage equals install reality (runtime + dev toolchain). |
| `sbom:backend` / `sbom:web` | security | CycloneDX SBOM artifacts for both stacks (`backend-sbom.cdx.json` from the lock, `web-sbom.cdx.json` from `package-lock.json`), wired into `artifacts:reports:cyclonedx`. **Non-gating** (`allow_failure`): inventory must never block a fix from shipping. |
| `backend:build` / `web:build` | build | Container image (default branch) / production `next build`. |
| `backend:migrate-check` | migrate-check | `alembic upgrade head` → `downgrade -1` → `upgrade head` against a fresh database — every migration must have a working downgrade. |
| `web:lockfile` | lint (utility) | Non-gating helper: regenerates `package-lock.json` **in CI** when `web/package.json` changes, because the npm registry is proxy-blocked in the agent sandbox; the lockfile travels via job artifact/trace. |

### 1.1 Dependency supply chain (issue #5)

- `backend/pyproject.toml` ranges are the **intent**; the hash-pinned lock
  `backend/requirements.txt` is the **artifact**. CI and deploys install from
  the lock with `pip install --require-hashes -r requirements.txt` (then the
  first-party package with `--no-deps --no-build-isolation`); nothing outside
  the reviewed lock can ever execute. `backend:lock-drift` gates sync.
- Regenerate the lock (uv `0.12.5`) whenever `pyproject.toml` dependencies
  change:
  ```
  cd backend && uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements.txt
  ```
  Runtime and dev extras are locked **together** in one file: every backend
  CI job needs the dev toolchain anyway, one artifact means one drift
  surface, and Dependency Scanning then covers the CI toolchain too.
- **Policy: security-critical packages are exact-pinned** — the Playwright
  precedent (`@playwright/test` is pinned `1.62.1`, not ranged). The same
  applies to token/crypto-adjacent packages (`pyjwt`) and anything whose
  compromise reaches auth, money movement or CI execution: exact pin in the
  manifest, every bump its own reviewed MR. `renovate.json` enforces
  `rangeStrategy: pin` for these.
- `renovate.json` schedules weekly update MRs for **both** stacks (backend
  lock via the `pip-compile` manager, web via `package-lock.json`), so
  freshness is a reviewed event, not drift.

Known flake: one export-rendering timing test can trip on loaded runners.
If it is the only red and the change does not touch exports, re-run the
pipeline rather than weakening the guard (documented in `CLAUDE.md`).

## 2. Migration workflow

- Chain lives in `backend/migrations/versions/`, numbered sequentially.
  **Check the head before claiming a number** (`ls backend/migrations/versions/ | sort | tail -3`);
  parallel work claims numbers up front in the MR description.
- Migrations must be backward-compatible one release:
  **expand → backfill/migrate → contract**, staged across releases when a
  contract step exists.
- Every migration ships an **exact downgrade**; `backend:migrate-check`
  enforces it on every pipeline.
- Indexes ship in the same MR as the query that needs them, with EXPLAIN
  evidence in the MR description.
- Schema changes update the ERD (and any affected diagram) **in the same
  MR** — the diagram drift rule; `docs:spot-check` pins the ERD to the
  migration chain.

## 3. Environment flags

All configuration is environment-only (`backend/src/genesis/settings.py`);
no literal secrets anywhere (secret-detection CI enforces).

| Setting | Default | Notes |
|---|---|---|
| `DATABASE_URL`, `REDIS_URL` | — | Connection strings. The runtime DB role must be non-superuser without BYPASSRLS. `REDIS_URL` is **required whenever `ENVIRONMENT != development`** — boot refuses an empty value (fail-closed guard `assert_redis_configured_outside_dev`), because the rate limiter's in-process fallback counts per worker and would silently weaken auth limits N-fold. |
| `JWT_SIGNING_KEY` | — | Access-token signing; unset fails requests loudly. |
| `OTP_PEPPER` | — | Keyed OTP hashing. |
| `CURSOR_SIGNING_KEY` / `CURSOR_KEY_VERSION` (+ `_PREVIOUS` pair) | — / 1 | Signed pagination cursors; boot **fails closed** on missing/short key material or a version collision. Rotation = dual-version window. |
| `AUTH_RATE_LIMIT_PER_MINUTE` | 60 | Auth endpoint rate limiting, atomic sliding window (per validated tenant + resolved client IP). |
| `AUTH_RATE_LIMIT_IP_PER_MINUTE` | 240 | Pure-IP backstop bucket on auth endpoints; applies regardless of the `x-tenant-id` header. |
| `MEMBER_READ_RATE_LIMIT_PER_MINUTE` | 120 | Per-credential sliding-window limit **shared across all five member READ routes** (`/member/me`, `/member/transactions`, `/member/loans`, `/member/loans/{id}`, `/member/statement`). Keyed on the decoded member token's tenant + credential id — never a header, never an IP (mobile CGNAT). Fail-closed on limiter outage (denied 429 with `Retry-After`), spent **before** the live-link DB re-check so an over-limit request opens zero DB sessions. Sanity-check the default against the member app's screen-load fan-out before production. |
| `TRUSTED_PROXY_IPS` | **empty (fail-safe)** | Comma-separated IPs of trusted reverse-proxy hops (Passenger on MochaHost). Empty = `X-Forwarded-For` is **never** trusted; when set, the auth rate buckets key on the forwarded client IP (rightmost untrusted, `ipaddress`-validated; malformed chains collapse to one shared bucket). Only list proxies you operate — a wrong entry lets that peer spoof client IPs. |
| `EXPORT_ROW_CAP` / `EXPORT_BATCH_SIZE` / `EXPORT_ARTIFACT_TTL_HOURS` | 10000 / 500 / 24 | Export bounds, server-resolved only. |
| `IDEMPOTENCY_RETENTION_HOURS` | 24 | Idempotency replay window. |
| `DASHBOARD_SERIES_MONTHS` / `DASHBOARD_GUARANTOR_CAP` | 6 / 20 | Dashboard scan bounds. |
| `DEV_OTP_DISPLAY` | **false (fail-closed)** | DEV-ONLY on-screen OTP; **must be removed before staging** — see [security-model.md](security-model.md#6-dev-only-otp-display-flag). |

Health probes: every service exposes `/healthz` (liveness) and `/readyz`
(dependencies checked).

## 4. Development constraints

- **Proxy-blocked sandbox**: npm and PyPI installs are blocked in the agent
  sandbox. Do not retry installs; fix from the CI job trace and let CI
  arbitrate. Lockfile updates are produced *by CI* (`web:lockfile`).
  Time-box every network-touching command (< 120 s); use shallow, targeted
  git fetches only.
- **CI is the sole arbiter**: claims of green tests/builds require an
  observed pipeline in this project. Pipeline ids from before a repository
  re-import do not resolve — re-verify citations at the current project
  path.
- Never hand-edit generated files (`openapi.json`, `schema.d.ts`); never
  use interactive whole-file edits on files over ~2000 lines — scripted,
  single-asserted replacements only, verified by re-read + line-count check
  before committing (see [contributing.md](contributing.md)).
- Commit and push every coherent unit immediately; plain-merge `main` into
  feature branches; never rebase or force-push.
