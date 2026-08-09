# Genesis Prestige

## Documentation

- [`docs/MASTER_PROMPT.md`](docs/MASTER_PROMPT.md) — the engineering gates governing every MR.
- [`docs/diagrams/`](docs/diagrams/) — architecture diagrams (P-DIAG series); [`docs/diagrams/lock-order.md`](docs/diagrams/lock-order.md) is the **authoritative lock-ordering DAG** (v1.2 rule 11).

## Running on GitHub

The repository is usable interchangeably on GitLab and GitHub. GitLab CI
(`.gitlab-ci.yml`) is the **single source of truth**; the workflows under
[`.github/workflows/`](.github/workflows/) mirror it job-for-job (see
[CI parity](#ci-parity-github--gitlab) below for the exact map and the
documented gaps).

**Getting the code onto GitHub** — either:

1. **Push mirroring (recommended)** — GitLab → *Settings → Repository →
   Mirroring repositories* → add
   `https://github.com/<owner>/<repo>.git` as a **push** mirror,
   authenticating with a GitHub fine-grained personal access token
   (`Contents: Read and write` on the target repo). GitLab then pushes
   `main`, branches and tags automatically, and the GitHub workflows fire on
   each mirrored push. Note: mirrored pushes trigger `push` events only —
   PR-only jobs (`dependency-review`) run when actual pull requests are
   opened on GitHub.
2. **Manual dual-remote** —
   `git remote add github https://github.com/<owner>/<repo>.git` and push
   `main`/tags/branches explicitly.

**Required GitHub secrets/variables** — **none** for the mirrored jobs. The
GitLab pipeline uses only predefined `CI_*` variables plus non-secret inline
values, all of which are carried inside the workflow files themselves
(`POSTGRES_*` service config for the throwaway test databases, the
superuser/app-role `DATABASE_URL`s, and `NEXT_PUBLIC_TENANT_ID` — a public
test value per gate 1.6). There are **no** application secrets in either CI
config (MASTER_PROMPT §1.6: secrets live in CI variables / a secret manager,
and nothing in these pipelines needs one). Optional extras if you want them:

| Secret / setting | Needed for |
|---|---|
| `SEMGREP_APP_TOKEN` | Only if switching `semgrep scan` to `semgrep ci` with the Semgrep AppSec platform |
| `GITLEAKS_LICENSE` | Only if switching the gitleaks job to `gitleaks/gitleaks-action` on an organization repo |
| Dependency graph enabled | `dependency-review` job (on by default for public repos; enable manually on private ones) |
| Registry credentials (e.g. `GHCR` via `GITHUB_TOKEN`) | Only if a `backend:build` image-publish mirror is ever added (currently not mirrored — see gap register) |

## CI parity (GitHub ↔ GitLab)

`.gitlab-ci.yml` is authoritative. Any change to it MUST be reflected in
`.github/workflows/` in the same MR, or the divergence recorded here.

### Job map

| GitLab job | GitHub workflow / job | Parity |
|---|---|---|
| `docs:diagrams` | `docs-diagrams.yml` / `docs-diagrams` | Full — same image (`minlag/mermaid-cli:11.4.2`), verbatim script, same `docs/diagrams/**` trigger |
| `docs:spot-check` | `docs-spot-check.yml` / `docs-spot-check` | Full — `python3 docs/diagrams/c4-spot-check.py` + `python3 docs/diagrams/erd-spot-check.py`; PR trigger paths mirror the GitLab MR `rules:changes` (`docs/diagrams/**`, `backend/src/genesis/api/app.py`, `backend/migrations/versions/**`); push-to-main is unconditional (no `paths:` filter), mirroring the GitLab default-branch rule |
| `backend:lint` | `backend.yml` / `backend-lint` | Full — `python:3.12-slim`; `ruff check`, `ruff format --check`, `mypy --strict src`, `lint-imports` |
| `backend:test` | `backend.yml` / `backend-test` | Full — `postgres:16-alpine` service, migrate as owner, non-superuser `genesis_app` role so **RLS is actually enforced**, `pytest --cov=src --cov-fail-under=85`, EXPLAIN `perf/explain_*.txt` + junit artifacts |
| `backend:migrate-check` | `backend.yml` / `backend-migrate-check` | Full — `alembic upgrade head` → `downgrade -1` → `upgrade head` on a fresh database |
| `web:lint` / `web:test` / `web:build` | `web.yml` / `web-lint` → `web-test` → `web-build` | Full — `node:22-alpine`, same commands, same `needs` chain, `.next` artifact kept 1 week |
| `web:e2e` | `web.yml` / `web-e2e` | Full — `mcr.microsoft.com/playwright:v1.62.1-noble`, real production build, Playwright report/test-results uploaded on failure |
| `web:spec-drift` / `web:client-drift` | `web.yml` / `web-spec-drift`, `web-client-drift` | Full — verbatim scripts, including the client-drift **falsifiability negative proof** on every run |
| `semgrep-sast` (GitLab SAST template) | `security.yml` / `semgrep-sast` | **Approximate** — see gap register |
| `secret_detection` (GitLab template) | `security.yml` / `secret-detection` (gitleaks) | **Approximate** — see gap register |
| dependency scanning (Gemnasium template) | `security.yml` / `dependency-scan-python` + `dependency-scan-node` + `dependency-review` | **Approximate** — see gap register |
| `backend:build` | — | **Not mirrored** — see gap register |
| `web:lockfile` | — | **Not mirrored** — see gap register |
| `mobile:lint` / `mobile:test` | — | **Not mirrored** — see gap register |

GitLab `workflow:rules` (MR pipelines, default branch, tags) maps to
`on: pull_request` + `on: push` to `main`/tags; `rules:changes` maps to
`paths:` filters; `rules:exists` conditions are satisfied at every commit of
this repo, so those jobs run unconditionally on both platforms;
`default: interruptible: true` maps to per-workflow `concurrency` groups with
cancel-in-progress on PRs.

### Gap register (honest parity — GitHub coverage is NOT identical)

1. **SAST**: GitLab runs `semgrep-sast` with GitLab-maintained, curated
   rulesets. GitHub runs open-source semgrep with the public `p/default`
   ruleset. Rule coverage differs. Blocking behavior also differs: on GitHub
   findings fail the job (`--error`); on GitLab the job passes and findings
   feed the MR security widget + merge policies, which have no GitHub
   equivalent here.
2. **Secret detection**: GitLab's `secret_detection` analyzer and ruleset are
   replaced by gitleaks `v8.18.4` (default rules) over the full git history.
   Rulesets differ; gitleaks findings fail the job, GitLab's report-only job
   does not.
3. **Dependency scanning**: Gemnasium (GitLab's advisory database, lockfile
   analyzers, security widget) is replaced by `pip-audit` (backend, via the
   installed dependency closure) and `npm audit` (web) as **report-only**
   jobs, plus `actions/dependency-review-action` as a PR-time blocking gate
   for newly introduced vulnerable dependencies. Advisory databases and
   blocking semantics differ from Gemnasium's.
4. **No security dashboard**: GitLab's vulnerability report, MR security
   widget, and "critical vulnerabilities block merge" policy layer do not
   exist on GitHub in this setup; triage happens from job logs.
5. **`backend:build` not mirrored**: it is a Kaniko push to the *GitLab*
   container registry (`$CI_REGISTRY_IMAGE`), main-branch only —
   GitLab-registry-specific by design. A GHCR equivalent is deliberately out
   of scope until needed.
6. **`web:lockfile` not mirrored**: a GitLab-agent-sandbox utility
   (`allow_failure`, produces `package-lock.json` in CI and ships it via the
   job trace because the agent sandbox has no npm registry access). On
   GitHub runners it is meaningless.
7. **`mobile:*` not mirrored**: gated on `rules:exists: mobile/**/pubspec.yaml`,
   which does not exist at HEAD, so those jobs never spawn on GitLab either.
   Mirror them when the Flutter workspace lands.
8. **`docs:spot-check` scope**: ~~was `c4-spot-check.py` only~~ — CLOSED
   (!70): `erd-spot-check.py` is now wired into the GitLab job and mirrored
   here, and the push-to-main trigger is unconditional on both platforms
   (GitLab dropped the default-branch `changes:` filter in !70; the GitHub
   push trigger dropped its `paths:` filter in the same MR). PR/MR pipelines
   stay paths-scoped identically on both.
9. **Cosmetics without gate value**: GitLab's `coverage:` regex (MR coverage
   display) and junit MR widget have no GitHub equivalent — the actual gate
   (`--cov-fail-under=85`) and the junit XML artifact are mirrored.
10. **Verification status**: the GitHub workflows are schema-reviewed and
    mirror the GitLab scripts verbatim, but they cannot be execution-verified
    from inside GitLab CI (no GitHub runner is reachable from this
    environment). Treat the first GitHub run as the acceptance test.

## Getting started

To make it easy for you to get started with GitLab, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own. Want to make it easy? [Use the template at the bottom](#editing-this-readme)!

## Add your files

* [Create](https://docs.gitlab.com/user/project/repository/web_editor/#create-a-file) or [upload](https://docs.gitlab.com/user/project/repository/web_editor/#upload-a-file) files
* [Add files using the command line](https://docs.gitlab.com/topics/git/add_files/#add-files-to-a-git-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://gitlab.com/genesis-group8953131/genesis-prestige.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

* [Set up project integrations](https://gitlab.com/genesis-group8953131/genesis-prestige/-/settings/integrations)

## Collaborate with your team

* [Invite team members and collaborators](https://docs.gitlab.com/user/project/members/)
* [Create a new merge request](https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/)
* [Automatically close issues from merge requests](https://docs.gitlab.com/user/project/issues/managing_issues/#closing-issues-automatically)
* [Enable merge request approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/)
* [Set auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)

## Test and Deploy

Use the built-in continuous integration in GitLab.

* [Get started with GitLab CI/CD](https://docs.gitlab.com/ci/quick_start/)
* [Analyze your code for known vulnerabilities with Static Application Security Testing (SAST)](https://docs.gitlab.com/user/application_security/sast/)
* [Deploy to Kubernetes, Amazon EC2, or Amazon ECS using Auto Deploy](https://docs.gitlab.com/topics/autodevops/requirements/)
* [Use pull-based deployments for improved Kubernetes management](https://docs.gitlab.com/user/clusters/agent/)
* [Set up protected environments](https://docs.gitlab.com/ci/environments/protected_environments/)

***

# Editing this README

When you're ready to make this README your own, just edit this file and use the handy template below (or feel free to structure it however you want - this is just a starting point!). Thanks to [makeareadme.com](https://www.makeareadme.com/) for this template.

## Suggestions for a good README

Every project is different, so consider which of these sections apply to yours. The sections used in the template are suggestions for most open source projects. Also keep in mind that while a README can be too long and detailed, too long is better than too short. If you think your README is too long, consider utilizing another form of documentation rather than cutting out information.

## Name
Choose a self-explaining name for your project.

## Description
Let people know what your project can do specifically. Provide context and add a link to any reference visitors might be unfamiliar with. A list of Features or a Background subsection can also be added here. If there are alternatives to your project, this is a good place to list differentiating factors.

## Badges
On some READMEs, you may see small images that convey metadata, such as whether or not all the tests are passing for the project. You can use Shields to add some to your README. Many services also have instructions for adding a badge.

## Visuals
Depending on what you are making, it can be a good idea to include screenshots or even a video (you'll frequently see GIFs rather than actual videos). Tools like ttygif can help, but check out Asciinema for a more sophisticated method.

## Installation
Within a particular ecosystem, there may be a common way of installing things, such as using Yarn, NuGet, or Homebrew. However, consider the possibility that whoever is reading your README is a novice and would like more guidance. Listing specific steps helps remove ambiguity and gets people to using your project as quickly as possible. If it only runs in a specific context like a particular programming language version or operating system or has dependencies that have to be installed manually, also add a Requirements subsection.

## Usage
Use examples liberally, and show the expected output if you can. It's helpful to have inline the smallest example of usage that you can demonstrate, while providing links to more sophisticated examples if they are too long to reasonably include in the README.

## Support
Tell people where they can go to for help. It can be any combination of an issue tracker, a chat room, an email address, etc.

## Roadmap
If you have ideas for releases in the future, it is a good idea to list them in the README.

## Contributing
State if you are open to contributions and what your requirements are for accepting them.

For people who want to make changes to your project, it's helpful to have some documentation on how to get started. Perhaps there is a script that they should run or some environment variables that they need to set. Make these steps explicit. These instructions could also be useful to your future self.

You can also document commands to lint the code or run tests. These steps help to ensure high code quality and reduce the likelihood that the changes inadvertently break something. Having instructions for running tests is especially helpful if it requires external setup, such as starting a Selenium server for testing in a browser.

## Authors and acknowledgment
Show your appreciation to those who have contributed to the project.

## License
For open source projects, say how it is licensed.

## Project status
If you have run out of energy or time for your project, put a note at the top of the README saying that development has slowed down or stopped completely. Someone may choose to fork your project or volunteer to step in as a maintainer or owner, allowing your project to keep going. You can also make an explicit request for maintainers.
