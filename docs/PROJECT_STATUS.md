# EKIP — Project Status

Status: **Living document. Update this at the end of every milestone.** This file exists so the project can be picked back up — in a new AI conversation, or by you alone — without re-deriving context from scratch.

Last updated: 2026-08-26 (Phase 26)

**For the exact, ordered, executable sequence to close every remaining item
below, see `docs/operations/final-go-live-runbook.md`. For a plain
checklist, `docs/operations/production-release-checklist.md`. For the
formal PASS/BLOCKED-per-category verdict, `docs/operations/
FINAL_PRODUCTION_CERTIFICATION.md` — which currently reads `NOT PRODUCTION
READY`.**

---

## Status classification (read this first)

Four labels, used consistently everywhere below — do not read "the code is
correct" as "verified":

- **CODE COMPLETE** — the implementation, migration, or IaC change exists, is reviewed, and passes every check this environment can run (tests, typecheck, `alembic heads`, `az bicep build`). It has never been executed against real infrastructure.
- **VERIFIED** — actually executed in this environment (or via CI, with evidence) and confirmed correct. This is the only label that should ever be read as "done."
- **BLOCKED** — cannot be executed in this environment at all; the exact missing access is named, and a runbook phase exists to close it once that access is available.
- **REQUIRES HUMAN ACTION** — the code/plan is ready, but the next step is inherently something only a human with the relevant access can authorize or perform (a console rotation, an explicit "yes, apply this to Neon," a resource-group grant) — distinct from BLOCKED in that no further engineering closes it, only a decision or a credential.

| Area | Status |
|---|---|
| Backend implementation, tests, import boundaries | **VERIFIED** |
| Frontend implementation, typecheck/lint/build | **VERIFIED** |
| RBAC, application-layer tenant isolation | **VERIFIED** |
| RLS runtime-role fix (`ekip_app`, migrations `b8f3d6a1c4e7`/`c5e2a9f4d7b3`) | **VERIFIED** — applied to the real Neon database 2026-08-22; role confirmed live with `rolsuper=false, rolbypassrls=false, rolcreatedb=false, rolcreaterole=false`. See `docs/operations/migration-recovery.md`'s "Resolution" section for the exact executed sequence, including two real bugs (`NOSUPERUSER` clause Neon can't grant; a bare `CREATE FUNCTION` colliding with a pre-existing hand-created one) found and fixed live, not guessed around |
| RLS mechanism validation (`rls_isolation_test.py`) | **BLOCKED** — no disposable Postgres in this environment |
| RLS active in any real deployment | **REQUIRES HUMAN ACTION** — `ekip_app` is provisioned and ready; the application's own `DATABASE_URL` still connects as `neondb_owner`. Switching it, and re-verifying login/incident/audit reads under the new role, is the one remaining step — not yet done as of this update |
| Neon migration state (`alembic_version` orphan) | **VERIFIED — RESOLVED 2026-08-22.** `alembic current`/`migration_status.py` both confirm the database is at head (`c5e2a9f4d7b3`), `alembic check` reports no drift. This was the project's single longest-standing BLOCKED item |
| Credential rotation (Azure DevOps PAT, Neon, Redis) | **REQUIRES HUMAN ACTION** — procedure documented, needs console access only a human has |
| Azure/Bicep IaC | **CODE COMPLETE** — compiles clean, 3 real wiring bugs found and fixed this session |
| Real Azure deployment | **BLOCKED** — no EKIP-authorized resource group; declined to use the one unrelated resource group this identity can reach |
| Docker images/compose | **CODE COMPLETE** (static review only) |
| Docker execution | **BLOCKED** — no `docker` binary in this environment |
| CI/CD workflows | **CODE COMPLETE** — well-constructed; updated for the new migrations, and again in Phase 17.1 to add the deterministic-evaluation regression gate to `ci.yml`'s `backend` job |
| CI/CD actual run results | **BLOCKED** — no `gh`/GitHub API access in this environment. Every CI command has been run locally with its exact flags (Phase 17.1), but no GitHub Actions run has ever been observed |
| AI evaluation — deterministic (Mode 1, `app/evaluation/`) | **VERIFIED** — 28-case fixture suite actually executed (Phase 17/17.1): 18 pass, 10 negative controls correctly detected at their pinned stages, exit code 0; all six CI failure conditions simulated and confirmed to exit non-zero. Needs no database or API key |
| AI evaluation — live corpus (`scripts/eval_confidence.py`) | **BLOCKED** — no live corpus + funded `OPENAI_API_KEY`. Unchanged and untouched by Phase 17/17.1 |
| Full Playwright E2E | **BLOCKED** — no running stack; 0/0 executed |
| Backup/restore | **BLOCKED** — no provisioned database to back up |
| Accessibility/responsive | **CODE COMPLETE** for the fixes made this session; live browser/AT validation **BLOCKED** |
| Documentation | **VERIFIED** — this file, the runbook, the checklist, and the certification report are all current as of this update |

---

## Current baseline (verified this session)

- Backend: **487 tests passing**, 1 pre-existing unrelated failure deselected (`tests/ingestion_retrieval/test_connectors.py::test_one_connector` — a stale, unparametrized live-connector test broken independently of any work in this project's recent history; not touched, since fixing it would require deciding what live connector credentials it should actually run against).
- Import-linter: **7/7 contracts kept** (`app.api`/`app.core` cannot reach `app.ingestion`/`app.mcp` internals; `database` is a leaf; etc).
- Frontend: **typecheck PASS, lint PASS (0 warnings), production build PASS**.
- Alembic migration chain: single linear chain, head `c5e2a9f4d7b3`.
- `infra/main.bicep`: compiles clean (`az bicep build`, 0 errors), 15 resources including a previously-missing migration job.

## Completed phases

**Phases 1–3**: Foundation, module scaffold, core implementation (auth, incidents, postmortems, agents, ingestion, retrieval), security hardening (prompt injection defenses, SSRF guards on connector URLs, OAuth/PKCE, CORS), Azure Key Vault abstraction + envelope encryption for secrets at rest.

**Phase 4 (Deployment readiness)**: Dockerfiles (backend + frontend), `docker-compose.yml`, GitHub Actions CI (`ci.yml`, `e2e-and-eval.yml`, `main-extra.yml` — includes a `secret-scan` gitleaks job), Bicep IaC (`infra/main.bicep`), `/health` (liveness) + `/ready` (readiness, checks Postgres, reports Redis as degraded-not-failing) endpoints, Alembic migration diagnostics (`scripts/migration_status.py`). **Real Azure deployment has never been executed — no cloud access in this environment. Everything here is verified statically/locally, not against live Azure infrastructure.**

**Phase 4.5/4.6 (Migration & credential incident)**: Investigated a Neon `alembic_version` pointing at a revision absent from git history (root-caused to a lost/uncommitted merge from an abandoned branch); recovery migration `90ff736ced55` drops only confirmed-orphaned objects. A credential exposure incident (an Azure DevOps PAT and DB/Redis passwords briefly appeared in tool output during a `.env` audit) is documented in `docs/operations/security-incidents.md`; gitleaks was added to CI as the direct remediation. **Credential rotation and applying the pending recovery migrations against Neon both remain BLOCKED on explicit user authorization / console access — not yet done.**

**Phase 4.7/4.7B (RBAC/RLS)**: Fixed a confirmed `incident:read` authorization gap (seeded via migration `d706a360fc2a`, backfilled to every existing role). RLS architecture and a disposable-Postgres validation script (`scripts/rls_isolation_test.py`) exist. **Live RLS validation against a real PostgreSQL instance has never been executed — no local Postgres or Neon write access in this environment. This remains the single most important BLOCKED verification in the project**: RLS policies exist in migration `a1c3e9f2b7d4` and have been reasoned about carefully, but "the SQL looks right" is not the same as "it was tested against a real database with two tenants and a hostile query," and only the latter should ever be reported as PASS.
  **Re-confirmed and sharpened during this session's Phase 8.10 audit**: this is not merely "unvalidated," it is two separate, both-necessary gaps. (1) The RLS *mechanism* is unvalidated against a disposable database (as above). (2) Separately and more urgently: `DATABASE_URL` in every environment that has ever run this application connects as `neondb_owner`, which Neon confirms has `bypassrls=true` (`docs/operations/migration-recovery.md` line 190) — meaning **every RLS policy in the schema is currently inert against real traffic regardless of (1)**, because the connecting role sees every row unconditionally. Tenant isolation in the currently-running system relies entirely on the application-layer `WHERE organization_id = ...` filtering already present in every query, not on RLS as a backstop.

**Production closure phase — RLS remediation (this session, code-complete, not yet live)**: Read `f4a7c2e9b3d1` (the abandoned branch's own `ekip_app`-provisioning migration, recovered read-only via `git show` against `origin/simran-ekip` — never merged, never checked out) to determine the exact, already-reasoned-through intended architecture rather than re-deriving it from scratch. Adapted it onto `main`'s own current head as two new migrations:
- `b8f3d6a1c4e7` — provisions `ekip_app` (`NOSUPERUSER`/`NOBYPASSRLS`/`NOCREATEDB`/`NOCREATEROLE`), idempotent (converges an already-existing role, e.g. Neon's leftover copy, to this definition rather than failing), password read from `EKIP_APP_ROLE_PASSWORD` at migration-run time (never hardcoded), `ALTER DEFAULT PRIVILEGES` so every future table/function is automatically grantable without a follow-up migration.
- `c5e2a9f4d7b3` — a new `SECURITY DEFINER` function, `resolve_user_first_organization(uuid)`, closing a real, confirmed functional gap the audit surfaced: `user_roles` is one of `c7d4e8f19a2b`'s `FORCE ROW LEVEL SECURITY` tables, and `core.auth.service.login_with_password`'s org-resolution step (`get_first_organization_id`) queries it *before* any tenant context can exist — under the current `bypassrls` connection this silently works, but password login would break completely, for every user, the instant `DATABASE_URL` switches to `ekip_app` without this fix. `app/core/users/repository.py`'s `get_first_organization_id` now calls this function via the same `text("SELECT ...")` pattern `resolve_refresh_token_organization_id` already established for the identical bootstrap problem on `refresh_tokens`. 2 new regression tests (`tests/core/users/test_repository.py`), both passing.
`infra/main.bicep` gained the actual "migration database vs runtime database" split section 3 of this phase's own instructions asked for: a new `Microsoft.App/jobs` (`migrateJob`) resource — which turned out not to exist at all despite `scripts/deploy.sh` already calling `az containerapp job start --name "${NAME_PREFIX}-migrate"` against it, a real, confirmed gap found while building this — connects with the admin credential and runs `alembic upgrade head`; `backendApp`/`workerApp` now connect as `ekip_app` via a new `ekipAppPassword` parameter, never the admin credential. `docker-compose.yml`/`.env.docker.example` got the same split for local parity (migrate overrides `DATABASE_URL` to the local Postgres superuser; backend/worker default to `ekip_app`). `.github/workflows/main-extra.yml`/`e2e-and-eval.yml` updated with a CI-only placeholder `EKIP_APP_ROLE_PASSWORD` so the new migration doesn't break their existing disposable-Postgres migration runs. `docs/operations/deployment.md` and `security-incidents.md` updated with the full "migration vs runtime database" model and a post-rotation verification checklist.
**What this does NOT do, stated plainly**: none of this has been applied to Neon or any other live database — every statement above is `git`-committed migration/IaC code, verified via `alembic heads`/`az bicep build`/pytest, never executed against a real Postgres server of any kind (disposable or otherwise) in this session. Applying `b8f3d6a1c4e7`/`c5e2a9f4d7b3` to Neon, and separately switching Neon's actual `DATABASE_URL` secret to `ekip_app`, both remain **BLOCKED on explicit user authorization** — this phase made the fix ready to apply in one step once authorized, not the applying itself.

**Phase 8 (Production infrastructure, this session)**: Re-audited Docker/Compose/CI/Bicep/health/KMS/Redis/rollback against the section 8.1–8.13 checklist. Docker daemon is unavailable in this environment (no `docker` binary), so Dockerfile/compose review was static; the Azure Bicep CLI (`az bicep build`) IS available and was used to actually (not just visually) compile `infra/main.bicep` — 0 errors both before and after fixes. Found and fixed **3 real bugs** the previous "syntax-validated" pass missed (compiling cleanly is not the same as being semantically correct): `OPENAI_API_KEY` was stored in Key Vault but never wired into either container app's environment at all; `DATABASE_URL` was missing its password entirely; `REDIS_URL` had no access key at all (Azure Cache for Redis authenticates by key, not by managed identity). All three fixed via real Container Apps `secrets` blocks (`keyVaultUrl`-backed for the Key-Vault-resident secret, Bicep-computed `listKeys()`/interpolated values for the other two), plus a second, separate "Key Vault Secrets User" role assignment (distinct from the existing "Key Vault Crypto User" grant, which is for the connector-credential KEK only, not for reading plain secrets). None of these fixes have been exercised beyond the compiler — still never deployed to a real subscription. Confirmed via actual pytest execution (already part of the 485) that the `KMS_PROVIDER=local` + `environment=production` fail-closed guard is real and tested, not just reasoned about. Confirmed CI (`ci.yml`/`main-extra.yml`/`e2e-and-eval.yml`) already runs migration validation, `alembic check`, and real Docker builds against disposable CI-local Postgres — genuinely comprehensive, though this session had no `gh` CLI access to verify actual pass/fail history on GitHub's runners, only that the YAML is well-constructed.

**Phase 9 (Fresh API contract audit, this session)**: Found and removed one genuinely fictional contract that had survived the Phase 7 audit: `invokeMcpTool()`/`ToolTestDrawer` called a `/mcp/tools/{name}/invoke` endpoint that has never existed on the backend (the code's own comment admitted this) — UI-gated to mock-mode-only so it was never reachable in production, but "zero confirmed fictional API contracts" means removing the dead code, not just hiding it. Found the inverse problem too — real backend capability with no frontend surface: the backend supports 9 connector source types (`slack`, `teams`, `github`, `azure_devops`, `jira`, `confluence`, `sharepoint`, `runbooks`, `monitoring`) but the registration UI (`ConnectConnectorModal`) only ever offered 2 (github, slack). Built real Jira and Confluence registration forms against their actual documented config shapes (`{base_url, projects}` / `{base_url, spaces}`) — both were explicitly named in the master prompt's own connector checklist. **Not done**: Teams, Azure DevOps, and SharePoint registration UI — these three connector types were added to the backend outside this session's own audit scope (not in the original Phase 7.14 checklist) and remain a documented, real gap, not a fictional one.

**Phase 10 (Fresh security audit, this session)**: Verified (not just re-read) CORS (wildcard-with-credentials rejected by a settings validator, real test coverage), SSRF guards (`assert_safe_connector_url` wired into Confluence/Jira, the only two connectors with admin-configurable base URLs; GitHub/Slack/Teams/SharePoint hit hardcoded hosts so the guard doesn't apply there; Azure DevOps interpolates `organization` into a URL *path* on a fixed host, not the authority, so it isn't an SSRF vector either), prompt-injection defenses (`app/agents/prompt_safety.py` genuinely imported into 8 real agent modules, not dead code), and OAuth `redirect_uri` origin validation (`_assert_redirect_uri_allowed`, checked against `cors_allowed_origins` on both `begin_sso_login` and `complete_sso_login`). No new Critical/High issues found beyond what Phase 7's security-UX pass already fixed (SSO secret plaintext exposure) and what Phase 8.10 re-confirmed (the RLS bypass-role gap above).

**Phases 11/12 (AI evaluation, full E2E)**: Confirmed still BLOCKED, not attempted. `scripts/eval_confidence.py` requires a live database with real ingested data and a real, funded `OPENAI_API_KEY` (costs real money per run) — running it without both would either fail outright or require fabricating inputs, neither of which is acceptable. No Playwright execution attempted for the same reason as every prior phase: no Docker, no local Postgres, no running dev server in this environment.

**Phase 13/14 (Cleanup, final regression, this session)**: Repo-wide sweep (`app/`, `frontend/src/`, `scripts/`, `infra/`, `.github/workflows/`) for `TODO`/`FIXME`/`HACK`/stray `print`/dead code found nothing beyond what Phase 7 already cleaned up. Verified zero broken relative links across every `docs/**/*.md` and `README.md` programmatically. `README.md` was already accurate (correctly lists all 9 real ingestion connector types) and needed no changes. Final regression (re-run after every subsequent fix, including the RLS remediation migrations below, not just once at the start): 487 backend tests passing, 7/7 import-linter contracts, frontend typecheck/lint/build all clean, Bicep compiles with 0 errors.

**Production closure — handoff package (this session)**: Created `docs/operations/final-go-live-runbook.md` (15 ordered phases, A–O, each with command/expected result/failure condition/rollback/evidence-to-capture — an operator with real Neon/Azure/GitHub/Docker/Redis/OpenAI access can execute this without reading this project's history), `docs/operations/production-release-checklist.md` (every item unchecked as of this update, each mapped to a runbook phase), and `docs/operations/FINAL_PRODUCTION_CERTIFICATION.md` (the fixed-structure PASS/BLOCKED report, verdict `NOT PRODUCTION READY`). A second real-world finding surfaced while preparing the runbook's Azure phase: this environment's `az` CLI is authenticated with real subscription access, and the identity does hold Contributor on one resource group — `rg-nextcare-purview-demo`, an unrelated pre-existing demo project. Asked the user explicitly rather than assuming; **user confirmed: do not use it.** Azure deployment remains BLOCKED on a real, EKIP-authorized resource group, now documented precisely rather than as a blanket "no permissions."

**Phase 5 (Observability)**: Structured logging with request correlation (`RequestContextMiddleware`, structlog contextvars), OpenTelemetry tracing (`SimpleSpanProcessor` in dev to avoid a background-thread-outlives-pytest crash found during testing), AI usage/cost telemetry (`app/agents/telemetry.py`, `AgentExecution.model_used/prompt_tokens/completion_tokens/total_tokens`), `/observability/{agents,mcp,ingestion}` endpoints.

**Phase 6 (Reliability)**: Fixed a real bug where `ChatOpenAI`'s `timeout=None` disabled the SDK's own default (LLM calls had no timeout at all); added `command_timeout`/pool recycling to the DB engine; full-jitter backoff for retries; token-bucket rate limiting (`app/shared/rate_limiter.py`, with two real bugs fixed — burst capacity collapsing to 1 for per-minute rates, and cold-start under-crediting a brand-new bucket key) applied to `/auth/login`, `/auth/signup`, `/ask`, incident investigation, connector sync; per-organization AI cost budget enforcement (`app/agents/cost_budget.py`, opt-in via `max_organization_cost_usd_per_day`).

**Phase 7 (Product completion)** — this session's work:
- **Invitation acceptance (7.5/7.6)**: the previous `accept_invitation` endpoint only flipped a status flag — no account, password, role, or session was ever created. Added a real single-use hashed token (`Invitation.token_hash`, migration `1269a7b553a9`), `accept_invitation_with_password()` (provisions the user via the same pattern `signup()` uses, then issues a real session), full frontend flow (`AcceptInvitationPage`, an invite-link panel with copy button since EKIP has no email-sending), and 6 new backend regression tests. Playwright E2E for this flow is **written but never executed — no live backend/Postgres in this environment.**
- **Knowledge Review, Access Rules UI**: built against real, audited backend contracts (publish/reject/update documents; create/list/deactivate access rules — no invented PATCH/reactivate).
- **Organization switching (7.8)**: confirmed the backend has no multi-org-per-session capability at all (JWT bakes in exactly one `organization_id` at login; password-login resolves an arbitrary "first" org; no "list my organizations"/"switch active org" endpoint exists). Removed `TenantContext`'s `organizations`/`setOrganization` — they were exposed but had zero real callers and would have silently desynced the UI from the session's actual, immutable tenant if anyone had wired a button to them. **Not fabricated; documented as a real backend gap, not built.**
- **Full API contract audit (7.9, 7.20)**: found and fixed 4 confirmed frontend/backend mismatches — a fictional `Project.slug` field (backend has no such column; was rendering blank), a broken invite-role dropdown (offered "member"/"viewer" roles that don't exist server-side and always 422'd — only "admin" is ever created), a fictional `"generic_oidc"` SSO provider enum value, and missing AI token-usage/cost fields on `AgentExecutionStats`. Built the previously-nonexistent invitation list/revoke admin UI (a destructive `tenancy:manage` action with zero frontend surface before this).
- **Security UX (7.23)**: found and fixed a **critical** bug — the SSO client-secret field rendered as plaintext (`type="text"`, not `password`) with help text falsely claiming "actual secret values are never displayed" — and a related trap where the (correctly redacted) placeholder could be resubmitted to the create-only `configure_sso` endpoint, which has no update capability at all. Fixed by making an already-configured SSO's fields read-only rather than inventing a PATCH endpoint that doesn't exist.
- **UI state completeness (7.17)**: fixed a contradictory simultaneous "search failed" + "no results" render in `SearchPage`, missing loading/error handling on the dashboard's two breakdown charts, and silently-swallowed fetch failures in `TenantContext` (no `.catch()` — a real outage looked identical to "no organization").
- **Accessibility (7.18)**: `DropdownMenu` gained `aria-haspopup`/`aria-expanded`/Escape-to-close/arrow-key navigation; `Tabs` gained arrow-key navigation and real `tabpanel`/`aria-controls` wiring (both consumers, `AskPage` and `IncidentDetailPage`, updated); `TableSkeleton` now announces loading via `role="status"`; the mobile sidebar toggle gained `aria-expanded`; `ConnectConnectorModal`'s hand-rolled tab buttons (no ARIA semantics at all) were replaced with the real `Tabs` component. **Not done**: no codebase-wide `aria-invalid`/`aria-describedby` wiring for form validation (confirmed absent everywhere — forms rely on native HTML5 validation only, surfaced via toast on submit failure, not inline per-field messages). No live browser/screen-reader testing was performed — all accessibility work is static code review plus targeted fixes, not verified against actual assistive technology.
- **Responsive design**: not independently re-audited this session beyond what's implied by the existing Tailwind responsive classes already in place; no live-viewport verification was possible (no dev server/browser in this environment).
- **Code quality (7.21)**: deleted two confirmed-dead components (`IncidentCard.tsx`, `KnowledgeCard.tsx`, both fully superseded by `DataTable`-based rendering with zero remaining references) and a duplicate `AgentExecutionStatus` type. No `TODO`/`FIXME`/`console.log`/debug statements found anywhere in `app/` or `frontend/src/`.

## Known BLOCKED items (environment-limited, not skipped by choice)

| Item | Why blocked | What would unblock it |
|---|---|---|
| Live RLS *mechanism* validation | No local PostgreSQL, no Neon write access | A disposable Postgres+pgvector instance (or explicit Neon authorization) to run `scripts/rls_isolation_test.py` for real |
| RLS *actually active in production* | `DATABASE_URL` connects as `neondb_owner`, which has `bypassrls=true` — confirmed, not assumed. The fix (`ekip_app`, migrations `b8f3d6a1c4e7`/`c5e2a9f4d7b3`) is now written and ready — applying it is what's blocked. | Explicit user authorization to (a) apply the two new migrations to Neon and (b) switch Neon's `DATABASE_URL` secret to connect as `ekip_app` |
| Real Azure deployment | The Azure CLI here is authenticated, and this identity DOES hold Contributor on one existing resource group (`rg-nextcare-purview-demo`) — but it's an unrelated, pre-existing demo project, not an EKIP resource group. Declined to deploy into it; user confirmed "don't use it." No access to any EKIP-designated resource group or the subscription broadly. | A real, EKIP-designated resource group (new or user-granted) to actually run the Bicep templates against (now includes the previously-missing `migrateJob` and the runtime/migration credential split) |
| Credential rotation (Azure DevOps PAT, Neon password, Redis password) | No console access to the affected systems | Console/portal access to rotate each credential; full post-rotation verification checklist now in `docs/operations/security-incidents.md` |
| AI evaluation (`scripts/eval_confidence.py`) | Requires a live database with real ingested data and a real `OPENAI_API_KEY`; costs real money per run | A funded OpenAI API key + populated test database |
| Full Playwright E2E execution (invitation flow, and the full suite generally) | No backend/Postgres process running in this environment | A running backend + frontend dev server + seeded test database |
~~Applying pending migrations to Neon~~ | **DONE 2026-08-22**, with explicit user authorization — see `docs/operations/migration-recovery.md`'s "Resolution" section | — |
| CI actual pass/fail history | No `gh` CLI / GitHub API access from this environment | `gh run list`/GitHub web UI access to confirm the workflows have actually run and passed on real infrastructure |
| Docker execution (build + run the production-shaped stack) | No `docker` binary in this environment | A machine with Docker installed to run `docker compose up --build` and verify the migrate→backend/worker→frontend sequence for real |

None of the above have been faked, guessed, or reported as passing. Where a script/test exists but couldn't be run, it's marked BLOCKED, not PASS.

## Pending work (not started or not exhaustive)

- Deeper per-field accessible form validation (`aria-invalid`/`aria-describedby`) across settings forms — currently only native HTML5 validation + toast-on-failure.
- A from-scratch, in-browser responsive/accessibility pass (this session's work was static code review only).
- Phase 9's "second independent" API contract audit was folded into one thorough audit plus direct spot-verification of every fix in this session, not two fully separate audit passes.
- Phases 12 (full E2E) and 14 (final regression including E2E) cannot report real pass/fail numbers without a running environment — see BLOCKED table above.
- Switching the real database connection to a restricted, RLS-respecting role (`ekip_app` or equivalent) — currently `neondb_owner`, which bypasses RLS entirely. This is the single highest-impact remaining security gap and requires a live Neon change, not a code change.

## Phase 15 (Connector registration UI completion, this session)

Built the previously-missing Teams, Azure DevOps, and SharePoint registration
forms in `ConnectConnectorModal.tsx`, closing the gap Phase 9 documented
(backend supported all 9 source types; only GitHub/Slack/Jira/Confluence had
a frontend form). Config shapes were read directly from each connector's own
docstring, not guessed: `app.ingestion.connectors.teams` (`{"team_id":
"...", "channels": [...]}`, Graph bearer token), `app.ingestion.connectors.
azure_devops` (`{"organization": "...", "projects": [...]}`, a literal PAT),
`app.ingestion.connectors.sharepoint` (`{"site_ids": [...]}`, Graph bearer
token — no base URL field, unlike Jira/Confluence). Added the matching
`CreateTeamsConnectorInput`/`CreateAzureDevOpsConnectorInput`/
`CreateSharePointConnectorInput` types, `createTeamsConnector`/
`createAzureDevOpsConnector`/`createSharePointConnector` API functions (both
real and mock-mode paths), wired three new mutations into
`ConnectorsPage.tsx`, and extended `ConnectorCard.tsx`'s config summary to
cover the three new source types. `typecheck`/`lint` (0 warnings)/`build`
all re-confirmed passing after this change. **Not done**: no live backend to
verify an actual Teams/Azure DevOps/SharePoint connector registers and
syncs end-to-end — verified only that the frontend sends the documented
request shape, same environment limitation as every other frontend change
in this project's history.

## Phase 16 (Documentation consistency + organization-creation authorization gap, this session)

A full repository-wide verification pass (docs vs. code, plus a TODO/stub
sweep) found several claims that were stale — already resolved by earlier
phases but never updated in the doc/comment that made the original claim —
and one genuine, previously-undocumented authorization gap. Fixed both
categories; deliberately did **not** touch anything the pass confirmed was
already correctly implemented (pgvector, connectors, OTel, cost tracking,
rate limiting, RLS policies, encryption) or that requires a live-infra
decision (RLS runtime role, credential rotation, Azure/Docker/CI/AI-eval/E2E
execution — all still exactly as described above).

**Stale documentation fixed** (code was already correct; only the docs/comments lagged):
- `app/shared/schemas/identity.py`: module docstring and the `project_permissions` field comment both said "no code path populates it yet." False as of `core/users/service.py`'s `resolve_identity`, which populates it via `repository.get_project_permission_map` — updated to say so. Also removed a stray non-English inline comment on `Identity.for_agent` (`# qki agents bohot saare hai`) found while in the file — noise inconsistent with the rest of the codebase's documentation style, not describing anything real.
- `docs/ENGINEERING_DECISIONS.md` #004: appended a superseding note (per this project's own "amend, don't rewrite history" convention) correcting the "not yet populated" claim, rather than editing the original 2026-07-30 text.
- All 7 ingestion connectors (`base.py`, `github.py`, `slack.py`, `jira.py`, `teams.py`, `azure_devops.py`, `sharepoint.py`) plus `app/ingestion/schemas.py`'s `ResolvedConnectorConfig`: docstrings said `credential_ref` is "treated as literal... until `shared/security` exists." Verified against `app/ingestion/service.py`'s `_execute_ingestion_job`, which really does call `decrypt_secret(get_kms(), config_row.credential_ref)` before constructing `ResolvedConnectorConfig` — `shared/security` exists and is wired in; `credential_ref` genuinely is the final, decrypted plaintext by the time a connector sees it, not a temporary placeholder. Reworded all 8 docstrings to state this as the real architecture.

**Real gap fixed — `create_organization` had no authorization check**: `POST /organizations` (`app/api/routers/tenancy.py`) passed an already-authenticated `actor` through to `core.tenancy.service.create_organization` with no permission check at all — any authenticated user, in any organization, holding any or no permissions, could call it to create an arbitrary new organization. The function's own docstring already admitted this ("who/what is allowed to call this... is still not pinned down anywhere in the docs"), so this was a confirmed, self-flagged gap, not a surprise finding. Fixed by gating the `actor is not None` path with `require_permission(actor, "tenancy:manage")` — the same permission and helper every other mutating operation in this module already uses, per the module's own stated rule. The `actor is None` path (self-serve `signup()`, dev bootstrap scripts) is untouched: an organization doesn't exist yet at that point, so there's no valid Identity to check a permission against, and gating it would break legitimate self-serve signup. Two new tests in `tests/core/tenancy/test_service.py` (`test_create_organization_with_actor_requires_tenancy_manage_permission`, `test_create_organization_without_actor_bypasses_permission_check`). Full backend suite re-run after the change: 501 passed, 1 pre-existing unrelated error (`test_one_connector`, the same stale fixture issue already documented above), 7/7 import-linter contracts kept, `ruff check` on every touched file shows zero new issues.

**Qdrant documentation-vs-code mismatch, fixed**: `Architecture.md`, `PROJECT_PLAN.md` §8.2-8.4, `DATABASE_DESIGN.md`, and `PROJECT_STRUCTURE.md` all described pgvector and Qdrant as two live, interchangeable, per-collection backends. `app/retrieval/qdrant/` is genuinely an empty placeholder package (confirmed — `app/retrieval/interfaces/base.py`'s own docstring already said as much) and `Settings.default_vector_backend` is never read anywhere. Chose Option B (correct the docs, don't build Qdrant speculatively): added explicit "current status: pgvector only, Qdrant not built" callouts to all four docs, reworded present-tense "both backends sit behind..." language to conditional "would sit behind... once built," and fixed `PROJECT_STRUCTURE.md`'s per-folder descriptions to say `qdrant/` is an empty placeholder rather than "Qdrant-backed implementation."

**`docs/USER_TESTING_GUIDE.md` found to be a stale snapshot, disclosed**: this guide was written 2026-08-06 and predates most of Phases 4-16 above. It independently (and correctly, at the time) flagged the Qdrant gap — its own "Stale documentation to distrust" section previously pointed at `PROJECT_STATUS.md` as the less-reliable doc, which is now backwards: `PROJECT_STATUS.md` has been kept current since, while this guide has not. It also repeated the now-fixed `project_permissions`-never-populated claim, and claimed `core.tenancy.service`'s organization/project/SSO/access-rule/invitation functions have "zero REST/MCP surface" — false, `app/api/routers/tenancy.py`'s `admin_router` exposes all of them. Added a prominent banner disclosing this, corrected the specific disproven bullets directly verified this session (rows 28 and the `core.tenancy.service` line in §3.3), and left every other claim in the guide as an explicitly time-stamped 2026-08-06 snapshot rather than re-auditing the entire document (which would be its own separate, large QA pass — not attempted here).

**Verified, not changed** (confirmed already correct during this pass, so left alone): Redis is used only for `arq` job queues, no response/semantic caching exists; `ChatOpenAI` is constructed in exactly one place (`app/agents/llm.py`'s `get_llm()`) with no model-routing concept yet; no `Conversation`/`UserPreference`/session-continuity model exists (only `AgentExecution`, an audit-style question log, backs `GET /ask/history`); `office_extraction.py` supports exactly `.pdf`/`.docx`/`.xlsx`, no OCR/image handling anywhere; no SCIM code exists anywhere; the audit *trail* is real and append-only (`core/audit/service.py`) but there is no audit *export* (CSV/JSON download) anywhere, only the existing list/query endpoint; no typed entity graph exists beyond the `Incident`→`IncidentTimeline`/`Postmortem` relationships already in `core_models.py`.

**Not done in this pass** (each is a genuinely large, separate initiative — scoping/building any of these without a design checkpoint first would risk exactly the kind of speculative rewrite this project's own conventions warn against): an evaluation harness for retrieval/grounding/confidence quality; GDPR/data-subject deletion; agent memory; a knowledge graph; proactive/pattern-detection intelligence; an investigation-agent reflection/critique stage; semantic caching; multimodal (image/OCR) ingestion; dynamic model routing; SCIM provisioning; audit export; empirically-driven (vs. placeholder) confidence/grounding thresholds. See the conversation/roadmap discussion for a proposed phased order — evaluation harness first, since it's the prerequisite for validating any change to the confidence/grounding layer.

## Phase 17 (Evaluation harness — `app/evaluation/`, this session)

Built the deterministic (Mode 1, CI-safe, no external API/DB) evaluation
harness flagged as Priority 2 above. Explicitly complements, does not
replace, two pre-existing live-only harnesses discovered during discovery
and left completely untouched: `scripts/eval_confidence.py` (confidence-
threshold sweep against a real ingested org + funded OpenAI key) and
`tests/rag_validation/run_validation.py` (grounded/negative-control question
grading with an LLM judge). Neither can run without live infrastructure;
this package's whole value is running without any.

**Architecture**: `app/evaluation/{schemas,runner}.py` plus `datasets/`
(JSONL + `.meta.json` loader/validation), `metrics/` (`retrieval.py` —
Recall@K/Precision@K/MRR/coverage, all pure functions, configurable K;
`grounding.py` — deterministic concept-traceability and citation
resolution/support/count checks, no LLM; `confidence.py` — calibration
bucket analysis, distinct from `eval_confidence.py`'s threshold sweep;
`investigation.py` — evidence coverage, hypothesis matching/support,
hallucinated-support detection), `assertions/answer.py` (all 7 required
types: exact_match/contains/contains_any/contains_all/forbidden_content/
regex/semantic_similarity, the last with a deterministic token-overlap
fallback and an optional real-embedding adapter), `adapters/` (pluggable
retrieval/answer/investigation/LLM-judge/semantic-similarity seams — a
`Fixture*` implementation for Mode 1 and a `Real*` implementation per
adapter for Mode 2/3, each `Real*` wrapping the actual unmodified EKIP
function it stands in for), `reporting/` (console + JSON), `fixtures/` (a
small synthetic auth-service-outage corpus plus 4 JSONL datasets: 12
retrieval cases, 6 grounding, 5 answer, 5 investigation — deliberately
engineered so the tagged negative-control/deliberate-failure cases fail and
everything else passes, proving the harness detects real problems rather
than trivially passing).

**A genuine "reuse, don't duplicate" integration**:
`adapters/eval_confidence_report.py` reads `eval_confidence.py`'s own real
JSON report and derives calibration pairs from actual production runs —
this package's calibration analyzer can grade real historical data with
zero live-pipeline code duplicated.

**Run it**: `python scripts/run_evaluation.py` (all 4 fixture datasets,
deterministic mode, exit code non-zero on any failure — a real CI gate
candidate) or `uv run pytest tests/evaluation/`.

**Verified this session**: `python scripts/run_evaluation.py` actually
executed — 18/28 fixture cases pass, exactly the 10 tagged
negative-control/deliberate-failure cases fail, each in the correct
retrieval-vs-generation stage (confirmed case-by-case, not just aggregate
counts). 98 new tests in `tests/evaluation/`, all passing. Full backend
suite re-run: 599 passed (up from 501; +98 evaluation tests, +1 fixed since
Phase 16 — same pre-existing unrelated `test_one_connector` error,
untouched). 7/7 import-linter contracts held (no contract needed for
`app.evaluation` itself — nothing outside it imports from it, and none of
the 7 existing "forbidden" contracts name it either as source or target).
`ruff check` clean on every new file (100-char line length enforced
project-wide; this took a dedicated cleanup pass after the initial
implementation).

**Mode 2/3 status, stated plainly**: `Real*Adapter`/`RealLLMJudge`
implementations exist and wrap the real, unmodified EKIP functions
(`retrieval.service.search`, `agents.service.answer_question`, `get_llm()`)
— they are code-complete, matching this project's own PASS/CODE-COMPLETE/
BLOCKED discipline, but have never been executed end-to-end in this
environment for the same reason `scripts/eval_confidence.py` itself hasn't
this session: no live database + funded `OPENAI_API_KEY` combination
available here. Only Mode 1 has actually been run and verified.

**Not done in this pass**: no CI workflow wiring yet (the script exists and
returns a real exit code, but `.github/workflows/*.yml` doesn't call it —
a deliberate stop-and-check point rather than editing CI without asking);
no empirical confidence-threshold retuning (this was explicitly out of
scope — "build measurement infrastructure, don't tune blindly"); the
fixture corpus is intentionally small (one scenario, ~28 cases total) per
this task's own "small, high-quality regression dataset, not a benchmark"
instruction — expanding it is easy (drop a new JSONL line + a fixture
corpus/canned-answer entry) but not attempted further here.

## Phase 17.1 (Deterministic evaluation wired into CI, this session)

Closed Phase 17's own "not done: no CI workflow wiring yet" item. The
harness now runs automatically on every PR and every push to `main`.

**The problem this had to solve first.** Phase 17's CLI exited non-zero
whenever any case failed — and the fixture suite deliberately ships 10
negative controls that are *supposed* to fail. Verified empirically before
changing anything: `python scripts/run_evaluation.py; echo $?` → `1`. Wiring
that into CI as-is would have made the build permanently red, and the
tempting "fix" would be deleting exactly the cases that prove the evaluator
detects anything. The exit code could not simply be suppressed either —
that produces a check that is green by construction and gates nothing.

**Fix: model expected outcomes explicitly, gate on expectation-matching.**
`EvaluationCase` gained `expected_outcome` (`"pass"` | `"fail"`, defaulting
to `"pass"` — the safe default, so a new case that starts failing breaks
the suite loudly) and `expected_failure_stage` (`"retrieval"` |
`"generation"`, valid only when `expected_outcome="fail"`; a validator
rejects the meaningless combinations rather than silently ignoring them).
`EvaluationResult` carries both verbatim — so the JSON artifact is
self-contained and judging a run never requires re-reading the dataset it
came from — plus `matched_expectation`/`regression_kind`, and
`EvaluationReport` gained `regressions`/`unexpected_failures`/
`unexpected_passes`/`wrong_stage_failures`/`expected_failure_count`/
`is_clean`. `passed` and `matched_expectation` are deliberately kept as
distinct questions: a negative control that correctly fails has
`passed=False, matched_expectation=True`. CI gates on the latter, never the
former. All four datasets bumped to `v1.1` with explicit per-case
expectations; the retrieval dataset's two `negative-control`-tagged cases
were retagged `absence-check`, since they assert something is correctly NOT
retrieved and therefore *pass* — the same tag meaning two different things
across datasets was a real trap.

Deliberately **not** done: the broader `grounding`/`investigation`/
`assertion` failure-stage vocabulary the task sketched. The runner only ever
emits `retrieval`/`generation`/`none`, and every category's failure genuinely
reduces to "evidence never retrieved" vs. "evidence was there and the system
still got it wrong". Declaring stages nothing can emit would be a fictional
contract — this repo's standing rule. A case's `category` already records
which evaluator ran; `stage` records where in the pipeline it broke.

**CI placement**: a step in `ci.yml`'s existing `backend` job (renamed to
"Backend tests + import-linter + deterministic evaluation"), *not* a new
workflow and *not* `e2e-and-eval.yml`. Rationale: this check needs none of
what the expensive tier needs (no live DB, no `OPENAI_API_KEY`, no service
containers, no secrets), and a separate job would rebuild the entire
dependency tree to run a ~10-second check. The JSON report uploads as the
`deterministic-evaluation-report` artifact with `if: always()` — a green
run's metrics are the baseline that makes slow quality drift visible, so
unlike the E2E workflow's failure-only Playwright artifact it is kept
unconditionally. `.gitignore` now excludes the generated report (while
deliberately continuing to track `scripts/eval_confidence_report*.json`,
which `--compare-to` needs as a committed baseline).

**Verified locally, by actually running each case — not by reading YAML**:
all six documented failure conditions were simulated and each confirmed to
exit non-zero — expected-pass case failing, negative control passing,
control failing at the wrong stage, malformed dataset, missing dataset file,
unwritable report path. The mirror case (same control, correct stage) was
confirmed to exit 0. The exact CI command (`uv run python
scripts/run_evaluation.py --report-path run_evaluation_report.json`) exits
**0** with all 10 controls intact; the exact CI pytest invocation (with its
`--deselect`) reports **629 passed, 1 deselected**. 128 tests in
`tests/evaluation/` (up from 98: +29 in the new `test_expected_outcomes.py`/
`test_ci_gate.py`, +1 in `test_runner.py`), where `test_ci_gate.py`
deliberately shells out to the real script as a subprocess rather than
calling `main()` — the thing CI depends on is the process exit status, and
only running the process proves it. 7/7 import-linter contracts; `ruff
check` clean. All three workflow YAML files re-parsed to confirm validity.

**Remaining limitation, stated plainly**: the workflow is *implemented and
locally verified*, not *observed passing on GitHub*. No GitHub Actions run
has occurred — this environment still has no `gh`/GitHub API access (the
same standing limitation `FINAL_PRODUCTION_CERTIFICATION.md` records for
CI/CD generally). "CI workflow implemented" and "CI workflow verified green
on real runners" remain different claims, and only the first is true.

## Phase 18 (Data lifecycle + data-subject deletion, this session)

Priority 3. Full ownership map and deletion architecture in the new
`docs/DATA_LIFECYCLE.md` — read that before touching `app/core/privacy/`.

**A real bug found and fixed during discovery** (the most valuable outcome
of this phase): `core.knowledge.service.reject_document` soft-deletes a
document via `documents.deleted_at`, and `core/knowledge`'s own reads filter
that column — but `retrieval/pgvector/store.py`'s two queries did **not**
(they joined `documents` only for title/source_url), and nothing purged the
derived rows in the three `*_chunks` tables, each of which holds its own copy
of the text plus its embedding. Net effect: **a human could reject a document
and the Answer Agent would still retrieve, quote and cite it.** Fixed with
two independent barriers, since either alone leaves a hole — a
`documents.deleted_at IS NULL` predicate on both the dense and lexical
queries (which also protects rows soft-deleted before this change), and an
actual chunk purge in `reject_document` using the pre-existing
`retrieval.service.delete` primitive, which turned out to exist with zero
callers anywhere in the codebase.

**Ownership findings that drove the design** (all from real FK constraints,
not preference): raw personal data exists in exactly **two** columns in the
whole schema — `users.(email, display_name)` and `invitations.email`; every
other person-reference is a surrogate UUID or a `"user:<uuid>"` tagged-actor
string that dereferences to the `users` row, so anonymizing that one row
neutralizes all of them without rewriting `audit_logs` (append-only by
explicit contract). The `users` row itself **cannot be deleted**:
`incidents.reported_by`, `postmortems.reviewed_by` and
`invitations.invited_by` are all `ON DELETE RESTRICT` — which is why this is
an anonymization architecture, not a row-deletion one. `connector_configs`
and `documents` have **no `user_id` column at all**, which settles the
"does the person who configured a connector own what it ingested" question
with evidence: they do not, so user deletion touches no organization
knowledge. `agent_executions.user_id` is `ON DELETE SET NULL`, i.e. the
schema already declaring that execution telemetry should survive
anonymized. Also confirmed: all user-attributable data is in Postgres (no
object storage anywhere; Redis holds only arq queues with org/connector
UUIDs), which is what makes the whole operation single-transaction.

**Implemented**: `app/core/privacy/{__init__,schemas,repository,service}.py`
— a plan/execute split where the dry run (`GET /users/{id}/data-deletion/plan`)
runs the *same* discovery code as execution rather than a parallel
reimplementation; per-step results so `partially_completed` is representable;
idempotent by construction (every mutation is a `DELETE/UPDATE … WHERE` that
matches zero rows on a second run, with `was_noop` as the observable signal);
every statement scoped by `user_id` **and** `organization_id`, with the org
always taken from `actor.organization_id` and never from a parameter. Session
revocation reuses the existing `core.auth.service.revoke_all_sessions` rather
than reimplementing it. `POST /users/{id}/data-deletion` added to the
existing `users.py` admin router under the same `tenancy:manage` convention
as `logout-all` — deliberately not `DELETE /users/{id}`, which would
advertise a row deletion that cannot happen. The audit event records counts
and status only, never the personal data just removed.

**Deliberately synchronous, with no `deletion_requests` table and no arq
job** — and documented as a reasoned choice, not a shortcut: user-scoped
deletion touches only small bounded per-user row sets and (correctly) no
documents/chunks/embeddings at all, so it fits in one transaction, which buys
atomicity and an immediately observable result that a queued job would give
up. Organization deletion is the case that genuinely needs the job model; it
is rejected at the service boundary with an explicit
`privacy.scope_not_implemented` error rather than silently doing something
partial.

**Verified**: 682 backend tests passing (up from 629; +48 in
`tests/core/privacy/` covering ownership classification, authorization,
cross-tenant prevention, SQL scoping, idempotency across three runs,
per-step partial failure, retry-after-failure, and derived-data/pgvector
cleanup, +5 API tests), 7/7 import-linter contracts, `ruff check` clean on
every touched file, and the Phase 17.1 CI evaluation gate still clean.

**Explicitly deferred as product/legal decisions** (declared in
`DeletionScope`, rejected loudly rather than half-done): `user_account`
scope (removing the actual row, given the three RESTRICT references),
`organization` scope (needs a knowledge-ownership decision plus background
execution), self-service deletion, legal retention enforcement, scheduled
purge (nothing expires anywhere in this system today — revoked refresh
tokens accumulate forever), and audit *export* (the trail exists; export
does not). Known limitations documented honestly in
`docs/DATA_LIFECYCLE.md` §7, including stateless access tokens surviving to
expiry, stdout logs containing user ids and (on two paths) raw emails,
backups being outside this code's reach, and external source systems being
untouched. **No GDPR compliance is claimed.**

## Phase 19 (Persistent agent memory, this session)

Priority 4. Full architecture in the new `docs/AGENT_MEMORY.md`.

**Discovery first, and it changed the design.** Verified across the whole
repository that EKIP had **no conversation or thread concept whatsoever**: no
LangGraph checkpointer, no `thread_id`/`session_id`/`conversation_id`
anywhere, nothing on `AskRequest` or `agent_executions`; every `/ask` is
fully independent and the graph is rebuilt per request by design. The only
cross-request record is `agent_executions` (a flat per-user log storing a
structured `input_summary`, explicitly not the raw prompt). So there was
nothing to duplicate -- and one scope became impossible rather than merely
unwanted: there is no `conversation` scope because there is no conversation
to scope to. `agent_executions` was left completely untouched.

**Implemented**: `agent_memories` (one table, migration `f1a2b3c4d5e6`) plus
`app/core/memory/{schemas,repository,service}.py`, a `/memories` router, and
a three-file agent integration. Two scopes only, each justified by machinery
that already exists: `user` (private, owner is `actor.user_id`) and `project`
(visible to holders of a `project_memberships` row, read from the
already-populated `Identity.project_permissions`). No new permission code.

**The invariant, and how it is enforced**: authorization lives in the SQL
`WHERE` clause of the same statement that orders by vector distance, so
Postgres evaluates it *before* `LIMIT` selects anything -- an unauthorized
row is never a candidate, not ranked-then-dropped. The "nearest N then
filter" shape was rejected explicitly: its intermediate set contains other
users' private memories, which leaks through `LIMIT`, timing, and the next
metric someone adds. Fails closed throughout -- an identity with no
`user_id` gets no user-scoped branch at all, and an identity eligible for
nothing gets an explicitly impossible predicate rather than an omitted
clause (omitting it would turn "sees nothing" into "sees the whole org").
RLS added in the same migration: this is the first table in the schema
holding rows private to an individual *within* an organization.

**Two Priority 3 lessons applied directly**: (1) the embedding is a
`Vector(384)` COLUMN on the memory row, not a child table, so orphaned
vectors are structurally impossible -- the exact failure mode that caused
the P3 retrieval leak; (2) deletion zeroes `content` and `embedding` rather
than only flipping a status, so two independent barriers exist.

**Two security decisions worth recording**: memory is injected into the
*untrusted* fenced evidence block, never `system_instructions` -- memory is
user-authored text, so the latter would be a prompt-injection channel into
the trusted system prompt, in a codebase that deliberately defends against
that. And memory is a separate `GraphState` field, never extra
`ScoredChunk`s, so it can never receive a `[n]` citation marker; grounding
verification still runs against retrieved chunks only. With no relevant
memory the prompt is byte-identical to before this feature (asserted by
test), and a recall failure degrades to "no memory" rather than costing the
user their answer.

**Deliberately narrow integration**: only `answer_question`. Postmortem,
knowledge-gap and investigation paths were left alone -- none has a
demonstrated need for a user's private notes, and injecting memory
everywhere would widen the prompt-injection surface for no product value.

**Privacy integration**: user-scoped memory is hard-deleted by
`core.privacy` (a real row DELETE, which removes the embedding with it);
project-scoped memory created by a departing person is retained, exactly as
their documents are. Audit events and `input_summary` record scope/type/
counts and content *length* only -- never memory text, since `audit:read`
and `GET /observability/agents` are both org-level surfaces.

**Verified**: 738 backend tests passing (up from 682; +46 in
`tests/core/memory/` covering creation validation, relevance ranking,
private/cross-tenant isolation in both directions, budget and limit
enforcement, update-reflected-in-recall, supersession exclusion, the
mandatory delete-then-unrecallable test, idempotent repeat deletion, and
agent-integration/degradation; +9 API tests). 7/7 import-linter contracts.
Single alembic head (`f1a2b3c4d5e6`); the repo's own migration-coverage
guard test confirms the table has a creating migration. Evaluation harness
extended with a `memory` category and a 10-case dataset (`memory_core_v1`):
`uv run python scripts/run_evaluation.py` reports 38 cases, **0
regressions, VERDICT CLEAN**, exit 0 -- including a negative control that
asserts one user recalls another's private memory and must fail, so a future
leak turns into a build failure rather than a greener report.

**Two vacuous tests found and fixed while here**: this FastAPI version
represents included routers as `_IncludedRouter` objects with no `.path`, so
`app.routes`-based assertions (including Priority 3's
`test_no_delete_verb_endpoint_exists_for_users`) were passing against an
effectively empty list. Both now introspect `app.openapi()["paths"]` and
assert non-vacuously.

**Deferred with reasons** (see `docs/AGENT_MEMORY.md` section 7): org-wide
shared memory (overlaps the existing reviewed-knowledge system -- product
decision), conversation scope (no conversation exists), automatic LLM-based
extraction (default path must not require a paid API), consolidation/
deduplication, automatic truth/contradiction/stale detection, private->shared
promotion, empirical threshold calibration, and any UI. No regulatory
compliance is claimed.

## Phase 20 (Permission-aware derived knowledge graph, this session)

Priority 5. Full architecture in the new `docs/KNOWLEDGE_GRAPH.md`.

**Discovery first, and it settled the entity/relationship vocabulary.**
Verified across every model file that EKIP has **no `service`/`system`/
`application`/`component` entity** -- the closest candidates
(`incidents.owner_team`, a nullable free-text label; `document_metadata`
EAV keys like `"repo"`) have no table, no id, and no lifecycle, so
manufacturing a node from either would be unauthorizable and undeletable.
Also verified: no `investigations` table (an investigation result is an
`incident_timeline` row with `event_type='investigation'`, not a separate
entity); `search_similar_incidents` computes similarity purely at query
time from vectors, so there is no stored incident<->incident relationship
today; **zero recursive-CTE precedent anywhere in this codebase**
(traversal is therefore iterative/programmatic, the path with actual
precedent); and the same soft-delete reality check Phase 18 already
surfaced for `documents` applies again here in the opposite direction --
`incidents.deleted_at`/`postmortems.deleted_at` are declared columns with
**zero reads and zero writes anywhere in the application**, so neither
entity type has an actual deletion path to hook physical graph cleanup
into yet (a documented limitation, not a gap in this feature).

**The architecture's one real decision**: split the relationship vocabulary
into `FOREIGN_KEY_RELATIONSHIPS` (never stored, resolved live from the
relational schema every time -- `has_postmortem`, `belongs_to`,
`investigated_by`) and `DERIVED_RELATIONSHIPS` (stored in one new table,
`knowledge_graph_edges` -- `documents`, read straight from
`document_metadata.source_incident_id`; `related_to`, the one
human-assertable relationship). Storing a copy of something Postgres
already enforces via foreign key would only add staleness and a leak path
-- literally the Priority 3 failure mode in a new guise. The least
stale-able derived data is derived data that is never stored.

**Implemented**: one table (migration `a7b8c9d0e1f2`, RLS applied in the
same migration per the Phase 19 convention) plus
`app/core/graph/{contract,schemas,repository,service}.py`, a
`/knowledge-graph` router (three endpoints: direct relationships, bounded
traversal, manual-relationship creation), and a lifecycle hook wired into
`core.knowledge.service.reject_document`.

**The invariant, and how it is enforced**: authorization is part of entity
*resolution*, never a post-filter. Every entity the graph can return -- the
traversal origin, every reached node, both endpoints of every relationship
-- passes through `_resolve_entity`, which re-fetches the row from its own
source of truth and re-applies that entity type's own existing read gate
(`incident:read`, the document published/`knowledge:review` rule, the
postmortem approved/`postmortem:write`/`postmortem:approve` rule). No new
permission code anywhere. Critically, this is applied to **both** endpoints
of every relationship, not just the traversal origin -- an edge naming an
entity the caller cannot see is silently dropped mid-traversal, never
surfaced. This is also what makes a deleted/invisible entity structurally
unable to leak through a derived edge: resolving it is exactly the step
that fails.

**Bounded, cycle-safe traversal**: hard-capped at depth 2
(`MAX_TRAVERSAL_DEPTH` -- a caller-supplied `depth` can only narrow this,
never widen it, enforced both by FastAPI/Pydantic and again inside the
service), node/edge caps enforced during expansion with an explicit
`truncated` flag, and structural cycle protection via a BFS `visited` set
(a node is only ever expanded once). Deterministic ordering throughout --
`foreign_key`/`deterministic_extraction` before `manual`, then a stable
tiebreak -- never a ranking model.

**Two lifecycle barriers**, the same discipline Phase 19's own memory
deletion established: query-time exclusion (`_resolve_entity` re-fetches
and re-authorizes on every read, so a stale row is inert on its own) plus
physical cleanup (`remove_edges_for_entity`, wired into `reject_document` --
the one real deletion path that exists for any entity type this graph
covers today) plus a self-repair half in the deterministic discovery pass
(`discover_document_incident_edges` deactivates any edge whose document or
incident no longer resolves).

**Evaluation harness extended, no second runner**: a `"graph"` category, a
`FixtureGraphAdapter`/`RealGraphAdapter` pair reusing the real
`Identity.has_permission` rather than reimplementing authorization, a
two-organization fixture graph, and a 6-case dataset
(`graph_core_v1.jsonl`) covering direct recall, multi-hop traversal with
depth-cap enforcement, a permission negative control, a deleted-entity
negative control (checked even at full permission level), cross-org
isolation, and one deliberately-wrong-expectation regression control.

**Verified**: 791 backend tests passing (up from 738; +53 across
`tests/core/graph/` [contract validation, dedup/revival upsert branching,
tenant isolation, project-permission isolation, deleted-source, deleted-
target, provenance preservation, multi-hop traversal, depth-cap
enforcement, cycle protection, edge-cap truncation, manual-relationship
creation and its dual-project permission requirement, deterministic
discovery and its repair half, lifecycle cleanup], `tests/api/
test_graph_router.py`, and `tests/evaluation/test_graph_adapter.py`). 7/7
import-linter contracts kept. Single alembic head (`a7b8c9d0e1f2`). The
migration-coverage guard test confirms the table has a creating migration.
`uv run python scripts/run_evaluation.py` reports 44 cases (up from 38),
**0 regressions, VERDICT CLEAN**, exit 0.

**One real gap found and fixed while wiring lifecycle integration**:
`reject_document`'s existing tests (`tests/core/knowledge/test_service.py`,
`tests/core/privacy/test_derived_data_cleanup.py`) passed `session=None`
and had no reason to anticipate a new call into `core.graph.service` --
both needed a monkeypatch added for the new hook. Not a regression in
either test's own intent, but a reminder that a lifecycle hook added to an
existing, already-tested function must update that function's tests, not
just add new ones for the new code.

**Deferred with reasons** (see `docs/KNOWLEDGE_GRAPH.md`'s "Known
limitations" and "Agent / retrieval integration" sections): reverse
fan-out from `project` (unbounded without a paging design not built here);
physical cleanup for incident/postmortem deletion (no deletion path exists
for either entity type in this codebase to hook into yet); Investigation
Agent integration (evaluated per the spec's own instruction to evaluate
rather than assume -- every existing evidence source is citable, and
graph relationships must not become fake evidence, so this needs its own
non-citable channel analogous to Phase 19's memory integration, not a
same-pass addition); a scheduled discovery job (no evidence justified a
new always-running sync system for one relationship type -- the pass is
invokable directly instead).

## Phase 21 (Proactive intelligence & pattern detection, this session)

Priority 6. Full architecture in the new `docs/PROACTIVE_INTELLIGENCE.md`.

**Discovery first, and it ruled out most of the obvious pattern types.**
Re-confirmed Phase 20's finding that no `service`/`system`/`component`
entity exists; additionally ruled out `incidents.owner_team` as a grouping
key (nullable free text with no canonical identity -- two spellings of the
same team would silently split into two findings), "repeated investigation
outcomes" (investigation results are free-text-heavy JSONB, not structured
enough to group deterministically), and semantic-similarity-based detection
(`search_similar_incidents` is unthresholded, non-deterministic embedding
search across every retrieval collection -- not a stored per-incident
signal, and this priority's own spec says to defer what can't be made
robust). What survived: `incidents.severity`+`project_id` (real, canonical,
already-indexed columns) and the Phase 20 graph's stored `document
--documents--> incident` edge (real, deterministic, already-indexed). Two
finding types, both single-project-scoped, both zero-LLM.

**The architecture's one real decision, mirroring Phase 20's own**: a
proactive finding is an *inference* -- `core.graph.contract.ProvenanceType`
deliberately has no `"inferred"` value (nothing in that module infers
anything), so storing a detected pattern as a graph edge would mean
fabricating a provenance kind that contract was explicitly designed not to
support. Findings get their own table pair
(`proactive_findings`/`proactive_finding_evidence`, migration
`b1c2d3e4f5a6`) instead. The graph is still reused correctly: its stored
`documents` edges bound candidate discovery for one detector, but the
edge is never treated as evidence -- the resolved `incident`/`document`
rows are.

**Implemented**: `app/core/proactive/{contract,schemas,repository,
service}.py`, two deterministic detectors (`recurring_incident_severity`,
`incident_multi_document`), an `/insights` router (list + detail, read-only,
no "detect now" endpoint), and background integration into the *existing*
`app.agents.workers` process -- no new scheduler, no new worker, no new
queue. A second cron job (every 6 hours) alongside the Knowledge Gap
Agent's existing daily one, on the same `arq:queue:agents`.

**The invariant, restated a third time**: authorization is part of
resolution, never a post-filter -- the same principle Phase 19 established
for memory and Phase 20 for the graph. Detection itself is unscoped (reads
real source state directly, the same "system-level maintenance pass" shape
`discover_document_incident_edges` uses); every caller-reachable read
independently re-resolves and re-authorizes each evidence entity against
its own existing gate (`incident:read`, document published/`knowledge:
review`) before it can ever appear in a response.

**Mixed visibility, handled deliberately**: a finding's stored
`support_count` reflects the full, unscoped detection-time count. A caller
who cannot see every piece of evidence never sees that number, the title/
summary implying it, or the finding at all if what they *can* see falls
below the finding type's own threshold once support is recomputed
narrower, per caller -- verified directly by a test asserting a
2-document finding becomes invisible to a reader lacking `knowledge:review`
(support recomputes to 1, below the threshold of 2) and reappears, fully,
once they gain it.

**Two lifecycle barriers, same discipline as every prior derived-data
priority**: query-time exclusion (`_resolve_evidence` re-fetches and
re-authorizes on every read) plus physical cleanup where a real hook
exists (`reject_document` now also calls `proactive_service.
handle_evidence_entity_removed`, which deletes the stale evidence row and
recomputes: below threshold → deactivated, still supported → count
updated). **Honestly documented, not invented around**: incidents have no
deletion path anywhere in this codebase (`incidents.deleted_at` is dead
code, the same fact Phase 20 already found), so there is no physical hook
for incident evidence yet -- query-time exclusion is what protects reads
in the meantime, and the hook is additive whenever a real deletion path is
built.

**Failure isolation, tested directly**: one detector's exception is caught
and reported in its own `ReconciliationResult.error`; it never aborts the
other detector's run and never deactivates any existing finding of any
type -- a crash must never be mistaken for "no patterns exist." Verified by
a test that makes one detector raise and asserts a previously-active
finding from the OTHER detector's run stays untouched.

**Idempotency, the mandatory test**: run detection once, run it twice
against identical source state, verify convergence -- one finding, an
unchanged evidence-row count (not doubled). `replace_evidence` deletes-
then-reinserts a finding's whole evidence set on every upsert specifically
so membership drift still converges rather than accumulating.

**Verified**: 839 backend tests passing (up from 791; +48 across
`tests/core/proactive/` [contract, upsert/reconcile branching, detector
triggers including stale-edge exclusion, the idempotency test,
reactivation, failure isolation, cross-tenant isolation, permission
isolation, mixed-visibility recompute-and-hide, the lifecycle hook],
`tests/api/test_insights_router.py`, and `tests/evaluation/
test_proactive_adapter.py`). 7/7 import-linter contracts kept. Single
alembic head (`b1c2d3e4f5a6`); migration-coverage guard confirms both new
tables have a creating migration. Evaluation harness extended with a
`"proactive"` category and a 7-case dataset (`proactive_core_v1.jsonl`):
`uv run python scripts/run_evaluation.py` reports 51 cases (up from 44),
**0 regressions, VERDICT CLEAN**, exit 0 -- including a negative control
that asserts a finding is visible to an identity with no permissions and
must fail, proving the leak-detection mechanism actually works.

**One real gap found and fixed while wiring lifecycle integration, same
class as Phase 20's**: `reject_document`'s existing tests needed a new
monkeypatch for the new `handle_evidence_entity_removed` call, exactly as
they did for Phase 20's graph-edge-removal hook -- a lifecycle hook added
to an already-tested function must update that function's tests too, not
just add new ones for the new code. A second, real design fix made during
testing: `handle_evidence_entity_removed` initially mutated an ORM row and
called `session.flush()` directly inside `service.py`, bypassing
`repository.py` entirely -- caught by a unit test passing `session=None`
(the established convention across this whole test suite), fixed by adding
`repository.update_support` and routing the write through it like every
other mutation in this module.

**Deferred with reasons** (see `docs/PROACTIVE_INTELLIGENCE.md`'s
"Remaining limitations"): semantic/LLM pattern detection (no deterministic,
thresholded similarity signal exists yet to build on); threshold
calibration on production data (both thresholds are placeholder initial
values); statistical anomaly detection; outbound notifications (no
notification infrastructure exists anywhere in this codebase); automatic
investigation/remediation creation; physical cleanup for incident deletion
(no deletion path exists to hook into); Investigation Agent integration
through a non-citable context channel (evaluated per the spec's own
instruction to evaluate rather than assume -- every existing evidence
source is citable, and a finding must not become fake evidence, so this
needs its own channel analogous to Phase 19's memory integration, not
built in this pass); a UI/dashboard.

## Phase 22 (Investigation Agent reflection & critique, this session)

Priority 7. Full architecture in the new `docs/INVESTIGATION_CRITIQUE.md`.

**Discovery first, and it settled where critique lives architecturally.**
Confirmed the investigation graph (`agents.graph.build_investigation_graph`)
is a single LangGraph node whose Python closure already orchestrates
multiple internal steps sequentially (gather evidence -> generate
hypotheses -> attach to timeline) -- the established precedent in this
exact agent for a bounded, no-cycle sequence, not "one graph node per
logical step." Also confirmed: `investigation.hypothesis._validate_
hypotheses` already strips every fabricated evidence reference before a
hypothesis is ever constructed (not just "at least one real citation," as
the spec assumed might be true) -- so a citation-existence structural
check would have been redundant, and only two genuinely new deterministic
dimensions (`insufficient_information`, `overconfidence`) plus one genuinely
new semantic one (does the evidence's CONTENT actually support the claim,
not merely does the citation exist) were worth building. Also confirmed:
no grounding node exists in the investigation graph at all (only the
Answer Agent has one, `agents.answer.grounding.verify_grounding` --
directly reused as this priority's template); no per-node OpenTelemetry
tracing exists anywhere in `app/agents/`; and cost accounting is already
automatic per graph execution, needing zero new wiring for critique's own
LLM calls.

**The architecture's one real decision**: critique is implemented as
additional bounded steps inside the EXISTING investigation node's closure,
not as new LangGraph nodes with conditional edges. This keeps the compiled
graph structurally identical to before this priority (one entry node, one
edge to `END`) -- there is no cycle construct at the graph level for a bug
to ever turn into an infinite loop, because none exists at all. The
alternative (new nodes + conditional routing) would have meant threading
ephemeral critique state through `GraphState` for branching logic nothing
outside this one node needs to observe.

**Implemented**: `app/agents/investigation/critique.py` -- two
deterministic structural checks (no LLM) and one bounded semantic critique
LLM call, combined into a single orchestration function
(`review_investigation`) with a hardcoded, linear, loop-free control flow:
one critique pass, and only if that pass says "revise," exactly one
revision attempt followed by exactly one more validating critique pass. A
second "revise" verdict on the revision has nowhere left to go and is
treated as "reject" -- never a silent "accept."

**The invariant, restated a fourth time, in a new shape**: this priority's
central rule is not "authorization is part of resolution" (already
established three times over) but its sibling -- *a critique may challenge
a hypothesis, but must never become a new source of evidence.* Enforced
structurally: `critique.py` holds no `session`, no `Identity`, no
`organization_id` -- it cannot fetch anything, so it cannot leak anything,
by construction rather than by a runtime check that could be bypassed.
Verified directly by a test asserting the critique's rendered prompt
contains only the `evidence`/`hypotheses` explicitly passed in.

**The second invariant -- never claim reviewed when review failed, was
skipped, or couldn't validate its own output**: `InvestigationResult`
gained `review_status` (`"not_reviewed"`/`"reviewed"`/`"review_failed"`),
distinct from `critique_verdict`. Malformed critique JSON, an unrecognized
verdict value, a critique-model failure, or a revision-generation failure
all degrade to `"review_failed"` with the best available (pre-critique or
pre-revision) hypotheses preserved -- never silently upgraded to
`"reviewed"`.

**Bounds are code constants, not settings, on purpose**:
`MAX_CRITIQUE_PASSES = 2`/`MAX_REVISION_ATTEMPTS = 1` live in `critique.py`
itself, deliberately NOT `Settings` fields, so no configuration change --
accidental or malicious -- can widen them into an unbounded loop. Only the
softer behavioral thresholds (kill switch, evidence-count floor,
overconfidence threshold and its citation-count pairing) are configurable,
following this codebase's established `Field(default=..., ge=...,
description=...)` settings convention exactly.

**No schema change.** Investigation results were already `incident_
timeline` rows with `event_type="investigation"` (confirmed, not a
parallel persistence system) -- the four new review fields are additional
plain JSON keys in the same JSONB `event_data` dict `record_investigation_
result` already writes. No migration, no new table.

**Verified**: 864 backend tests passing (up from 839; +25 in `tests/
agents/investigation/test_critique.py` covering structural validation,
malformed-output handling, fixed-penalty application, and full end-to-end
orchestration -- every required negative control: unsupported hypothesis,
supported hypothesis with no unnecessary revision, the revision bound,
malformed output, critique failure, insufficient-evidence rejection
without spending an LLM call, and the evidence-boundary/authorization-
isolation proof). 7/7 import-linter contracts kept. No Alembic change (head
unchanged at `b1c2d3e4f5a6`). Evaluation harness extended -- no second
runner, no new category (critique is a quality gate on the EXISTING
`investigation` category's own output): `ExpectedInvestigation.critique` +
a `CritiqueAdapter`, 5 new dataset cases (accept, reject, review_failed,
revise-then-accept, one deliberately-wrong-expectation regression
control). `uv run python scripts/run_evaluation.py` reports 56 cases (up
from 51), **0 regressions, VERDICT CLEAN**, exit 0.

**Deferred with reasons** (see `docs/INVESTIGATION_CRITIQUE.md`'s
"Remaining limitations"): production-calibrated thresholds and penalty
weights (all initial, reasoned values); advanced/cross-evidence
contradiction analysis (only what one critique pass directly observes in
one call is ever reported -- no independent structural contradiction
engine, since repository discovery found no trustworthy data shape to
build one on); multi-step autonomous reflection (the ceiling is one
revision, structurally, not a configurable depth); a human review
workflow; user-feedback-driven learning; long-term critique analytics; a
semantic live-quality benchmark (requires a funded model API and belongs
in this project's existing integration/live evaluation tier, not this
deterministic pass). Memory, the knowledge graph, and proactive findings
were deliberately NOT wired into critique -- none is citable evidence in
this codebase, and feeding any into critique would mean critique reasoning
over something never validated as evidence, exactly what this priority's
spec forbids; a future integration needs its own non-citable channel,
mirroring Phase 19's `memory_context`, not attempted here.

## Phase 23 (Semantic evaluation, live quality benchmarking & threshold calibration, this session)

Priority 8. Full architecture in the new `docs/SEMANTIC_BENCHMARK.md`.

**Discovery first, and it changed the shape of the work.** Two substantial
pieces of live/Tier-3 evaluation infrastructure already existed before this
priority started: `scripts/eval_confidence.py` (confidence-threshold +
grounding-rate calibration against real `test-org` data, with its own
threshold sweep and `--compare-to` regression gate) and
`tests/rag_validation/` (retrieval/grounding/citation PASS-FAIL validation
with a deliberately separate LLM judge). Both were already wired into
`.github/workflows/e2e-and-eval.yml`, secret-gated, skip-clean. Neither was
rebuilt. What was genuinely missing: a structured, multi-dimension
answer-quality rubric (not a single "score 1-10"), and a baseline-vs-
reflection A/B benchmark for Priority 7's critique/reflection loop --
nothing previously measured whether critique actually helps.

**Implemented**: `app/evaluation/semantic/` (`schemas.py`,
`answer_quality.py`, `investigation_ab.py`, `calibration.py`,
`fixtures.py`, `runner.py`) + `scripts/run_semantic_evaluation.py`, a new
Tier 3 CLI entry point. Deliberately a separate Pydantic contract set from
Tier 1's `EvaluationCase`/`expected_outcome` -- a semantic quality score is
a measurement, not a defied-or-matched deterministic prediction, and
blurring the two vocabularies was the one thing section 14 of this
priority's spec was most explicit about avoiding. Tier 1
(`scripts/run_evaluation.py`) is completely unchanged: still 56 cases, 42
pass + 14 negative controls, `VERDICT: CLEAN`, exit 0, verified by a fresh
run this session.

**Investigation A/B methodology**: hypotheses are generated exactly
**once** per case; that draft is `baseline`, never critiqued.
`reflected` is `agents.investigation.critique.review_investigation`
applied to that *same* draft -- not a second independent generation. This
isolates "did critique review this" from LLM sampling variance by
construction, and is literally the real production code path
(generate -> critique), called directly rather than simulated. Outcome
classification (`critique_improved`/`critique_correctly_rejected`/
`critique_damaged`/`critique_no_measurable_change`/`critique_unavailable`)
is an explicit, honest **structural proxy** -- it compares critique's
action against `critique.validate_structurally`'s deterministic findings
on the baseline, not a human or independent ground-truth judgment of
semantic quality, which this package does not have.

**Calibration has a structural sample-size floor, and it caught two real
mislabeling bugs while I was building the threshold inventory.**
`DEFAULT_MINIMUM_SAMPLE_SIZE = 20`: below this, a result can never be
`"calibrated"`, however clean the numbers look. Re-expressing
`scripts/eval_confidence.py`'s own last report against
`Settings.confidence_threshold` correctly produced `insufficient_data`
(n=14, real `test-org` data). While building the rest of the threshold
inventory, I initially labelled `confidence_signal_weights`
(`agents.confidence._SIGNAL_WEIGHTS`) and `Settings.
memory_relevance_threshold` as `"intentionally_fixed_domain_rule"` --
wrong, caught on review: both settings' own docstrings already say they
are honest, uncalibrated **placeholders** (`ENGINEERING_DECISIONS.md`'s
"Open" section for the former; the field's own `description=` for the
latter), not intentional design choices. Corrected both to
`"insufficient_data"` before this priority was reported done -- exactly
the failure mode ("a constant, casually described as reasonable, never
revisited") this whole priority exists to prevent, this time caught
inside the priority's own new tooling rather than shipped past it.

**Evaluator limitations, documented rather than hidden**: this codebase
has one configured LLM provider, so the answer-quality rubric is graded by
the same model family it evaluates. Methodology controls in place:
faithfulness is graded against supplied evidence only (never real-world
truth), correctness prefers a human `reference_answer` when supplied,
every dimension requires a non-empty `reason`. A genuinely new limitation
was **found, not theorized**, by the first live run (see below): the
rubric has no "appropriate refusal" concept -- when the system correctly
returns `NO_ANSWER` to an out-of-domain question, every dimension scores
`0.0`, because "the answer doesn't address the question" is technically
true even though declining *was* the correct behavior. Documented in
`docs/SEMANTIC_BENCHMARK.md`, not silently patched around on a 3-case
corpus.

**Two genuine live runs actually executed** in this environment against
real `gpt-4o-mini` and (for the second) real ingested `test-org` data,
bounded via `--limit`: run 1 (`--limit 2`, synthetic only) cost $0.0008 /
11.6s total LLM latency, 0 errors, `VERDICT: BASELINE_ESTABLISHED`; run 2
(`--limit 2 --repository-derived`) cost $0.0015 / 20.7s, 0 errors, same
verdict. Full breakdown, including the exact per-case answer-quality
judgements, in `docs/SEMANTIC_BENCHMARK.md`'s "Results" section. This is
real measured evidence, not a framework-only validation claim -- but 2-6
cases per category is far below this package's own 20-example calibration
floor, so neither run supports a production-quality verdict beyond
"the harness works end to end against a real model and real data."

**CI**: extended the existing `.github/workflows/e2e-and-eval.yml`
(secret-gated, nightly/push-to-main/manual -- never added to the fast
PR-blocking `ci.yml`) with a new `semantic-benchmark` job, same gating as
the existing `ai-evaluation` job, `--repository-derived --limit 5`,
uploads the report as a 90-day artifact, fails the build only on
`verdict=="regression_detected"` (a real critique-caused A/B regression),
not on `insufficient_data`/`baseline_established` -- those are honest,
expected outcomes on this priority's still-small corpus. **This job has
not actually executed in hosted GitHub Actions** -- only local runs in
this session's own environment; `EVAL_DATABASE_URL`/`OPENAI_API_KEY`
secret provisioning for hosted CI is unverified.

**Verified**: 918 backend tests passing (up from 864; +54 in
`tests/evaluation/semantic/` covering dataset validation, evaluator
malformed-output handling, evidence-boundary/authorization isolation
mirroring Priority 7's own pattern, A/B equivalent-input pairing, every
outcome-classification branch, calibration's sample-size floor and margin
logic, cost/latency aggregation, and the real subprocess-level "missing
credentials fails loudly, no report written" contract). 7/7 import-linter
contracts kept. No Alembic change. `uv run ruff check` clean across the
new package, tests, and script.

**Deferred with reasons** (full list in `docs/SEMANTIC_BENCHMARK.md`'s
"Remaining Limitations" / this priority's own Final Report): no realistic
production corpus (only synthetic-controlled + repository-derived from an
already-small dataset exist; no `sanitized_real`/`manually_curated` cases
yet); insufficient sample size for every threshold examined (all
`insufficient_data`, by design -- this package refuses to claim
`calibrated` below its own floor); retrieval Recall@K/Precision@K/MRR not
built (would need a larger graded-relevance corpus than exists; Tier 1's
deterministic fixtures + `rag_validation`'s PASS-FAIL check already cover
what a 3-6 case corpus could meaningfully support); investigation critique
thresholds/penalties, proactive-detection thresholds, knowledge-graph
bounds, and Answer Agent grounding thresholds intentionally NOT swept
(reasons per-row in the threshold inventory table -- either genuinely
fixed structural bounds, or already covered by an existing separate-judge
check this priority chose not to duplicate); same-provider evaluator
limitation (see above); model nondeterminism across runs; no human
adjudication of any Investigation A/B outcome. Hosted CI execution of the
new `semantic-benchmark` job is unverified, per above.

## Phase 24 (Evaluation correctness, appropriate refusal & human-adjudicable benchmarking, this session)

Priority 9. Fixes a real correctness bug in Phase 23's own answer-quality
rubric, found by that phase's own live run: a correct `NO_ANSWER` refusal
scored `0.0` on every dimension, because the rubric assumed every good
answer is substantive. Full architecture and evidence in
`docs/SEMANTIC_BENCHMARK.md`'s new "Answer-mode contract" / "Rubric
architecture" / "Evaluator discrimination" sections.

**Root cause, confirmed by inspection before writing any code**: rubric
design, not prompt wording, aggregation, dataset limits, or missing
metadata alone -- the rubric had no concept that "I don't know" could
itself be the correct answer, and no benchmark case declared what the
evidence actually justified, so there was nothing to check the observed
behavior against. Also confirmed: `AskResponse` has no machine-readable
refusal field -- refusal is only ever inferable from text (two exact
sentinels: the model's own `NO_ANSWER` marker, and the Answer Agent node's
fixed `_INSUFFICIENT_GROUNDING_MESSAGE` fallback); production has no
"qualified/hedged answer" generation mode at all today (evidence classified
`"partial"` by the existing `agents.answer.sufficiency.SufficiencyVerdict`
is treated identically to `"insufficient"` -- both just decline);
`tests/rag_validation` already treats declining as PASS for its negative
controls but only as a coarse binary, with no abstention-quality grading.

**The answer-mode contract**: every `AnswerQualityCase` declares
`expected_answer_mode` (`"answer"` / `"qualified_answer"` / `"no_answer"`
/ `"unlabeled"`) -- a ground-truth statement about what the evidence
justifies, declared by the case author, never inferred from model output
(the self-grading trap section 5 of this priority's spec warns against).
Named after and directly grounded in the production `SufficiencyVerdict`
that already existed (`sufficient`/`partial`/`insufficient`) rather than
inventing new vocabulary. `ObservedAnswerMode` is classified in two
layers: refusal detection is DETERMINISTIC (exact match against the two
real production sentinels, reused via `is_no_answer`/
`_INSUFFICIENT_GROUNDING_MESSAGE`, zero LLM calls), while
substantive-vs-qualified is inherently semantic and is classified by the
SAME judge call that scores the substantive rubric's four dimensions --
one mode-aware call, never two. `outcome.classify_outcome_correctness` is
a pure, deterministic lookup (never model output) comparing expected
against observed, producing `correct`/`partially_correct`/`overconfident`/
`incorrect_refusal`/`critical_failure`.

**Two rubrics, not one reused rubric.** Substantive answers keep Phase
23's original four dimensions (correctness/relevance/usefulness/
faithfulness). Refusals get four dimensions built for abstention instead
(`abstention_correctness`/`unsupported_claim_avoidance`/
`explanation_quality`/`appropriate_next_step`) -- a refusal never receives
full credit merely for existing; `abstention_correctness` is what
separates a correct abstention from a lazy one (evidence clearly answers
the question, system declined anyway).

**Two deliberate, documented judgment calls in the outcome table** (not
obvious from the task's own worked examples, decided by checking this
codebase's own established philosophy first): a cautious decline on
merely-partial evidence is `correct`, not `incorrect_refusal` -- matching
`agents.answer.sufficiency`'s own motivating principle and `tests/
rag_validation/README.md`'s "a confidently wrong answer is the single most
damaging failure mode," not penalizing exactly the caution that codebase
already rewards. A hedged answer where the case declares the evidence has
NO bearing at all is still `critical_failure`, same tier as a fully
confident hallucination -- there's no partial credit for hedging about
nothing.

**Contrast fixtures test the EVALUATOR, not production.** Six new cases
(`CONTRAST_ANSWER_QUALITY_CASES`), three same-question/same-evidence pairs,
using a new `AnswerQualityCase.fixed_answer` field (judged directly, zero
`generate_answer` calls) so a specific failure mode can be tested without
needing to coax it out of a live model. Caught a real bug in my own first
draft during development: hand-authored refusal prose that doesn't match
either production sentinel silently routes to the wrong rubric -- fixed by
reusing the real `_INSUFFICIENT_GROUNDING_MESSAGE` sentinel verbatim in
the refusal-labelled fixtures instead of inventing new phrasing, and
locked in with a regression test
(`test_refusal_labelled_contrast_cases_use_a_real_detectable_refusal_sentinel`).

**Repository-derived loading extended** from Phase 23's `clear-answer`-only
behavior to all three `eval_confidence_dataset.json` categories, each
labelled with its corresponding `expected_answer_mode`
(`clear-answer->answer`, `ambiguous->qualified_answer`,
`no-information->no_answer`) -- grounded in what each category already
means to `eval_confidence.py` and to `SufficiencyVerdict`, not invented.
An unrecognized category loads as `expected_answer_mode="unlabeled"`,
never guessed; caught and fixed a real gap where the loader's first draft
silently DROPPED unrecognized categories entirely rather than loading them
unlabeled, via a test that constructed exactly that scenario.

**Genuine live evidence, not simulated** (`gpt-4o-mini`, this environment,
2026-08-25/26, ~$0.005 total across two runs): every one of section 10's
four required discrimination checks demonstrated on real judge output
using same-evidence contrast pairs -- correct answer (1.000) beats
hallucination (0.250, `critical_failure`); correct refusal (0.750,
`correct`) beats the same hallucination; correct answer (1.000) beats
incorrect refusal (0.425, `incorrect_refusal`); qualified answer (1.000)
beats overconfidence (0.250, `overconfident`). The sharpest single piece
of evidence: the SAME refusal sentence, judged against two different
evidence sets, scored `abstention_correctness=1.0` against genuinely
insufficient evidence and `abstention_correctness=0.2` against evidence
that clearly answered the question -- proof the judge reasons about the
evidence, not the refusal's own wording. The live run also reproduced a
real production concern outside any planted fixture: a repository-derived
`ambiguous`-category question (`python-version-conflict` -- literally
`agents.answer.sufficiency`'s own originally-motivating case) generated a
fully confident answer against conflicting real `test-org` evidence,
scoring `overconfident` -- direct evidence that `generate_answer` called
outside the sufficiency gate (exactly what this benchmark's own
architecture does) still reproduces the class of failure that gate was
built to prevent.

**One new limitation found by this priority's own live run, left open
rather than patched**: the deterministic refusal detector only recognizes
the two exact production sentinels. A live-generated case
(`aq-partial-evidence`) produced a free-text, honest non-answer ("the root
cause was never conclusively identified... therefore there is no
information to provide") that doesn't match either sentinel -- classified
`substantive_answer`, computed as `critical_failure`. Not patched: the
principled fix needs either a second LLM call or restructuring what the
substantive judge's mode field can return, and fixing it reactively
against the exact wording this one live run happened to produce would be
tuning the detector to a fixture, which this priority's own spec
explicitly forbids. Documented honestly in `docs/SEMANTIC_BENCHMARK.md`'s
"Evaluator limitations" #5.

**Calibration integrity preserved, not incidentally improved.** Fixing the
rubric bug and adding contrast cases changed nothing about calibration
status -- `confidence_threshold`, `confidence_signal_weights`, and
`memory_relevance_threshold` all remain `insufficient_data`, exactly as
Phase 23 left them (re-verified, not reset or re-labelled).

**CI**: `.github/workflows/e2e-and-eval.yml`'s `semantic-benchmark` job
(added in Phase 23) needed no gating changes -- still secret-gated,
still not in the fast PR-blocking `ci.yml`. `runner._decide_verdict` now
also fails the job on `critical_failure`, at the same severity as a
critique regression, so the job's existing pass/fail semantics
automatically extend to the new failure mode without a workflow edit.

**Verified**: 966 backend tests passing (up from 918 at the end of Phase
23's own count minus its 54 semantic tests, replaced/expanded to 100 --
net +46; +5 new files: `test_outcome.py`, `test_fixtures.py`, plus
substantial rewrites of `test_schemas.py`/`test_answer_quality.py`/
`test_runner.py` for the new contract). 7/7 import-linter contracts kept.
No Alembic change. `uv run ruff check` clean across every new/modified
file. Tier 1 re-verified unchanged: `uv run python scripts/run_evaluation.py`
still reports 56 cases, 42 pass + 14 negative controls, `VERDICT: CLEAN`,
exit 0.

**Deferred with reasons** (full list in `docs/SEMANTIC_BENCHMARK.md`'s
"Remaining limitations" / this priority's Final Report): the free-text
refusal-detection gap above; no repeated-trial statistics for evaluator
discrimination (every discrimination number is from one live run each);
no human adjudication of any judgment; still no realistic production
corpus beyond the small controlled + repository-derived sets; every
calibration entry remains `insufficient_data`, unchanged by this priority
on purpose (fixing the rubric or adding fixtures is not evidence toward
calibration); same-provider evaluator limitation, unchanged from Phase 23;
model nondeterminism; hosted CI execution of `semantic-benchmark` still
unverified (only local runs occurred).

## Phase 25 (Robust refusal detection & machine-readable answer outcome, this session)

Priority 10. Fixes the exact open limitation Phase 24 documented and left
unpatched (`aq-partial-evidence`'s free-text decline misclassification) --
not by expanding phrase matching, but by giving the semantic benchmark
access to the same authoritative decision production itself already makes.
Full architecture in `docs/SEMANTIC_BENCHMARK.md`'s new "Production
answer-outcome contract" section.

**Discovery confirmed the fix belongs in the pipeline, not in text
matching.** Traced the full path (`AskRequest` -> retrieval -> `agents.
answer.sufficiency.assess_sufficiency` -> generation -> `agents.answer.
grounding.verify_grounding` -> `AskResponse`) and found: (A) production
already has exactly the right-shaped signal --
`SufficiencyVerdict` (`sufficient`/`partial`/`insufficient`) -- computed
BEFORE generation ever runs; (B) the earliest authoritative "this should
not receive a substantive answer" decision is that sufficiency check,
inside `agents.answer.node._generate_and_verify`, not anything derivable
from output text; (C) `partial` and `insufficient` are currently
structurally identical in behavior (both decline) -- confirmed by reading
the code, not assumed; (D) an answer CAN still become a refusal later, via
grounding verification stripping every sentence after a "sufficient"
verdict and successful generation -- meaning the final outcome has to be
computed once, at the end of the sequence, never fixed early; (E)
`AskResponse` is never persisted to the database (confirmed by a
repo-wide grep) and the frontend TypeScript mirror is hand-maintained and
auto-camelCased at the fetch layer -- so an additive field is genuinely
zero-risk to existing consumers.

**The contract: `AskResponse.answer_mode: Literal["answered", "no_answer"]
| None = None`.** Two values, not three -- production has no
qualified-answer generation mode today (confirmed, not assumed: the
generation prompt only ever produces a full answer or the literal
`NO_ANSWER` marker, and the node treats `partial` exactly like
`insufficient`), so a `"qualified_answer"` value would have been a false
capability claim Phase 24's own spec explicitly warned against. Defaults
to `None`, never `"answered"` -- an unlabeled historical response might
well have BEEN a refusal, and defaulting it to "answered" would silently
lie about exactly the case this whole priority exists to get right.

**Single authority: `agents.answer.node.generate_answer_with_outcome`.**
Extracted from the existing `_generate_and_verify` (same sufficiency ->
generate -> grounding sequence, unchanged), now RETURNING an `AnswerOutcome`
instead of only ever raising on decline. `_generate_and_verify` itself
became a thin, retry-compatible wrapper around it, preserving `node()`'s
existing retry-on-any-decline behavior byte-for-byte (verified: `call_
with_retry` still retries on `_UngroundedAnswerError`, unchanged). The two
generic-failure fallbacks in `agents.service._run_graph_and_record`
deliberately leave `answer_mode` unset -- an infrastructure failure is not
a semantic refusal, and conflating the two would let a bug masquerade as
an epistemically-correct decline.

**Evaluator integration: the semantic benchmark now calls the SAME
authoritative function.** `app.evaluation.semantic.runner.
run_answer_quality_case`'s live-generation path (no `fixed_answer`) calls
`generate_answer_with_outcome` instead of bare `generate_answer`, and
passes its `AnswerOutcome.mode` to `judge_answer_quality` as a new
`known_mode` parameter (tier 1 of a 4-tier hierarchy: production outcome ->
reserved test-metadata tier, not currently populated -> legacy sentinel
matching, unchanged from Phase 24 -> semantic classification for the
substantive/qualified distinction only, which production still doesn't
make). `fixed_answer` contrast cases deliberately do NOT get this
shortcut -- no pipeline ran to produce an outcome for them, and short-
circuiting their detection would defeat their whole purpose as
discrimination tests.

**Live-verified, not just unit-tested.** Two genuine live runs against
real `gpt-4o-mini`, same 9-case answer-quality + 3-case investigation-A/B
corpus, before (Phase 24, 2026-08-25) and after (this phase, 2026-08-26)
this priority's changes: `aq-partial-evidence` went from
`observed_answer_mode="substantive_answer"`/`outcome_correctness=
"critical_failure"` to `observed_answer_mode="no_answer"`/
`outcome_correctness="correct"` -- the exact, direct, measured fix.
Overall run: cost $0.0024->$0.0028, latency 31.9s->44.6s, tokens
10,340->11,922 -- a real, honestly-measured increase from the added
sufficiency-check calls (partially offset, for genuinely-insufficient
cases, by generation being skipped entirely), not hidden or rounded away.
The judge-call contract itself is unchanged: still exactly one LLM call
per case for judging, confirmed by both a live count and a dedicated unit
test.

**Case 5 (textual ambiguity) locked in, not just assumed safe.** A
substantive answer merely mentioning refusal-shaped wording ("The system
cannot answer requests when the upstream connector is disabled") is NOT
misclassified -- this already worked (exact-sentinel matching, never
substring/regex), but is now covered by an explicit regression test
(`test_substantive_answer_with_refusal_like_wording_is_not_misclassified`)
rather than being an untested invariant.

**CI/tiering unchanged.** No workflow file touched this priority. Tier 1
deterministic evaluation re-verified unchanged (56 cases, `VERDICT:
CLEAN`). `semantic-benchmark` remains secret-gated, not fast-PR-blocking.

**Verified**: 987 backend tests passing (up from 966; +21: `tests/agents/
answer/test_node.py` is new -- the first direct test coverage
`agents.answer.node`/`sufficiency`/`grounding` have ever had in this
repository, covering every `AnswerOutcome` branch including the late-
grounding-failure precedence case; 4 new tests in `tests/api/
test_ask_router.py` covering `answer_mode` serialization, the `None`
backward-compatibility default, unchanged request-payload requirements,
and the OpenAPI schema's exact 2-value nullable enum shape; several new
tests in `tests/evaluation/semantic/test_answer_quality.py`/`test_runner.
py` covering the `known_mode` hierarchy, the `aq-partial-evidence` fix
end-to-end, and the preserved one-judge-call-per-case contract). 7/7
import-linter contracts kept. No Alembic change (confirmed unnecessary:
`AskResponse` is never persisted). Frontend `tsc --noEmit` passes with the
additive `answerMode` field on the hand-maintained TypeScript mirror.

**Deferred with reasons** (full list in `docs/SEMANTIC_BENCHMARK.md`'s
"Production answer-outcome contract" / this priority's Final Report): a
real qualified-answer GENERATION capability (documented as a future
product capability, not built -- production still only ever answers or
declines); the "explicit test metadata" tier of the detection hierarchy
(reserved, unpopulated -- no current fixture needs it); any improvement to
`is_refusal_text` for cases with no authoritative outcome available
(historical/legacy text-only data remains inherently ambiguous by
construction, not a gap this priority claims to have closed); a second
LLM call to semantically classify free-text declines in the no-outcome
case (would break the one-judge-call-per-case contract, not attempted);
hosted CI execution (still only local runs); repeated-trial statistics,
calibration, or human adjudication (out of this priority's scope, per its
own explicit non-goals).

## Phase 26 (Human-adjudicated benchmarking, ground-truth dataset & evaluator calibration, this session)

Priority 11. Closes the gap every prior semantic-evaluation phase left
open on purpose: the benchmark could say "the LLM judge thinks answer A is
better than answer B," never "a human determined answer A is actually
correct." Full architecture in `docs/SEMANTIC_BENCHMARK.md`'s new
"Human-adjudicated ground truth" section.

**Discovery decided the storage architecture.** `app/evaluation/` has
three existing precedents for version-controlled, file-based benchmark
data and zero precedent for a database table backing it -- ground-truth
annotations reuse that exact convention (one JSON file per dataset
version, appended by a new CLI script) rather than adding a migration, a
table, and an authorization surface for what is fundamentally the same
"reviewed fixture" pattern. This also means section 13's API/tenant-
isolation requirements are structurally inapplicable: no API, no
per-organization row, nothing to leak. A genuinely useful discovery along
the way: `scripts/eval_confidence_dataset.json` already carries its own
`evidence`/`expected_answer` fields, inline, already committed -- reused
directly to build 3 new `repository_derived` annotatable cases with zero
live retrieval, sidestepping section 12's evidence-drift concern
structurally rather than defensively.

**Direction is enforced by module boundaries, not convention.**
`annotation_store.py` has no function that accepts evaluator output as
annotation input -- only a human (via the CLI) constructs an
`AnnotationDecision`. `evaluator_validation.py` is the only module that
reads both an evaluator result and a ground truth, and only ever reads.
`outcome_correctness` is never a separate hand-picked human label -- it's
DERIVED via the exact same `outcome.classify_outcome_correctness`
function the automated evaluator's own outcome is computed through, so a
human and the evaluator are held to the identical decision rule.

**Two-reviewer model, taken literally from the spec.** `resolve_ground_
truth` implements exactly `single_review`/`agreed_review`/
`resolved_disagreement`/`unresolved_disagreement` -- unresolved
disagreement's `final_observed_mode`/`final_outcome` are `None`, and
`evaluator_validation.py` structurally cannot compare against a `None`,
so an unresolved disagreement can never silently become calibration
truth. A resolution never mutates the two original annotations; found and
fixed a real bug during development where the inter-annotator agreement
function computed `unresolved_disagreement_count` WITHOUT ever consulting
recorded resolutions -- a genuinely resolved case was being miscounted as
unresolved. Caught by writing a live end-to-end run before writing the
report, not by inspection.

**Severity model, not a weighted score.** `evaluator_validation._classify_
severity` names exactly the dangerous mismatch shapes section 10 gives
(evaluator normalizing a hallucination as correct, evaluator calling a
lazy refusal correct = critical; evaluator missing overconfidence = high)
and leaves everything else as a plain confusion-matrix entry -- an
evaluator being more cautious than a human is a real disagreement, not the
dangerous direction this benchmark most exists to surface.

**Calibration eligibility gates on FOUR things, not sample size alone.**
`calibration.evaluator_reliability_eligibility`: sample floor, then
whether the human labels THEMSELVES are demonstrated reliable
(inter-annotator agreement status -- trusting the evaluator against
unreliable ground truth would be circular), then whether the compared
cases actually cover the two dangerous outcome classes
(`critical_failure`, `incorrect_refusal`), then whether any severe
disagreement is present. This function structurally can never return
`"calibrated"` even on its cleanest possible input -- the ceiling is
`"provisional"`, matching this package's own "one measurement pass is
never enough" discipline, stated as a design fact rather than left
implicit.

**Honest provenance, not fabricated human validation.** 9 annotations
recorded across the full 9-case annotatable corpus (6 Priority 9 contrast
cases + 3 new repository-derived ones), every one labelled
`synthetic_controlled_annotation` -- one reviewer (`reviewer-1`) covering
all 9, a genuine independent second reviewer (`reviewer-2`) on 4 of them,
including one real, defensible disagreement (is a hedge that reports
conflicting values a `qualified_answer` or a `no_answer` in disguise?)
resolved by a third pseudonymous reviewer. **Zero `human_review`
annotations exist.** This is mechanism validation, not external human
validation, and is reported as exactly that everywhere it appears --
never implied to be more.

**Genuine live evidence, not simulated**: one bounded live run
(`gpt-4o-mini`, this environment, 2026-08-26, $0.0034) exercised the full
pipeline against real evaluator output and surfaced a real, interesting
disagreement: the resolved human ground truth called
`annot-repo-python-version-conflict`'s dataset-sourced hedge text a
correct `qualified_answer`; the SAME live evaluator run classified it as
`substantive_answer` (overconfident) instead. Answer-mode/outcome
agreement 8/9 (0.89); `status="insufficient_data"` throughout (n=9,
4 double-reviewed -- both below the 20-case floor), correctly, not
rounded up to look more finished than it is.

**CI**: no workflow changes needed or made. `.github/workflows/ci.yml`'s
existing fast job already runs `uv run pytest tests/ -q` -- every new test
in this priority is fully deterministic (no LLM call, no live DB), so it
is automatically covered by the existing fast, PR-blocking gate without
any wiring.

**Verified**: 1035 backend tests passing (up from 987; +48 across 4 new
files -- `test_annotation_store.py`, `test_ground_truth.py`,
`test_evaluator_validation.py`, plus extensions to `test_calibration.py`/
`test_fixtures.py` -- covering the annotation contract, independent
review/duplicate rejection, all four ground-truth states, the resolution-
consultation bug fix above, deterministic evaluator-vs-human comparison,
the severity model, and calibration eligibility gating). 7/7 import-linter
contracts kept. No Alembic change (no database touched). Tier 1
re-verified unchanged: 56 cases, `VERDICT: CLEAN`.

**Deferred with reasons** (full list in `docs/SEMANTIC_BENCHMARK.md`'s
"Human-adjudicated ground truth" section / this priority's Final Report):
no real `human_review` annotations (only mechanism-validating synthetic
ones); n=9 far below the 20-case calibration floor; only 4 cases
double-reviewed, only 1 disagreement ever exercised; investigation A/B has
no ground-truth mechanism yet (no deterministic-candidate equivalent to
`fixed_answer` exists for it); live-generated and `--repository-derived`
cases remain un-annotatable by design (candidate/evidence drift); the
file-based annotation store doesn't handle concurrent multi-reviewer
writes safely (documented, not solved); same-provider evaluator
limitation, unchanged from Phase 24; hosted CI execution unverified (only
local runs occurred, though the new tests need no secrets to run there
either way).

## Phase 27 (Confidence/grounding calibration review, this session)

Requested directly: make `app.agents.confidence`'s routing mechanism
"production-ready through an evidence-based calibration process," with an
explicit instruction not to blindly change `_SIGNAL_WEIGHTS`,
`_DENSE_SIMILARITY_FLOOR`/`_DENSE_SIMILARITY_CEILING`, or
`Settings.confidence_threshold` without measured evidence.

**Conclusion up front: no values were changed.** Every existing piece of
calibration infrastructure (`app.evaluation`, `app.evaluation.semantic`,
`scripts/eval_confidence.py`) was inspected and re-run rather than
rebuilt -- it already existed, in good shape, from Phases 22-24. What this
review added is two confirmed findings that change what today's evidence
actually supports, a stale-documentation fix, and a new regression-test
file protecting today's (unchanged) behavior against a future silent
drift.

**Baseline re-established (Tier 1, deterministic, no live dependency)**:
`python scripts/run_evaluation.py` -- `VERDICT: CLEAN`, 56 cases (42
passed, 14 negative controls correctly detected), unchanged from Phase
23/24's own count. Its console "Confidence calibration" section reported
n=9, calibration error 0.240 (buckets `[0.6-0.7) predicted 0.617 actual
0.333`, `[0.7-0.8) predicted 0.732 actual 0.500`, `[0.8-0.9) predicted
0.810 actual 1.000`).

**Finding 1 (genuinely new): that calibration-error number is not evidence
about `app.agents.confidence` at all.** Traced `report.calibration`'s nine
`(predicted_confidence, was_correct)` pairs back to their source:
`app.evaluation.runner`'s deterministic mode uses `FixtureAnswerAdapter`
(`app.evaluation.adapters.generation`), which returns HAND-AUTHORED
confidence numbers from `app.evaluation.fixtures.canned_generations` keyed
by case id -- by that adapter's own docstring, deliberately never derived
from a dataset or from calling `evaluate_confidence`. Confirmed by
`inspect.getsource`: `FixtureAnswerAdapter.generate_answer` contains no
reference to `app.agents.confidence` anywhere. So Tier 1's calibration
section measures whether the evaluation harness's OWN fixture authors
picked internally-consistent confidence numbers -- a real, useful check on
the harness, but zero evidence about whether `_SIGNAL_WEIGHTS`/floor/
ceiling are well-calibrated in production. Not previously stated this
plainly anywhere in the docs; now is (this entry, plus a pinning
regression test -- see below).

**Live evaluation status: confirmed BLOCKED, as already tracked.**
`scripts/eval_confidence.py` needs a live database with real data already
ingested into the `test-org` organization and a funded `OPENAI_API_KEY`.
This environment has neither (no `.env` file at all in the working
checkout; `DATABASE_URL`/`OPENAI_API_KEY` unset) -- consistent with every
prior phase's own finding. No live run was attempted; no results were
fabricated.

**Finding 2 (genuinely new, and the more consequential one): the four
checked-in `scripts/eval_confidence_report*.json` files are not just
below the 20-example calibration floor (already tracked, `insufficient_data`
in `docs/SEMANTIC_BENCHMARK.md`'s threshold inventory) -- they are STALE
relative to the current scoring formula, independent of sample size.**
All four (`eval_confidence_report.json`, `_before.json`, `_after.json`,
`_after_noinfo.json`) carry `generated_at` timestamps of 2026-08-13/14.
`app/agents/confidence.py`'s own module docstring documents two later,
real rewrites of the signals these reports were measuring:
`_normalize_rerank_score`'s calibration (2026-08-30 live trace) and
`_normalize_top_similarity`/`_distinct_source_count_signal`'s shape fixes
(EKIP audit 2026-09-02, findings 1 and 2 -- the fused-RRF-score bug and the
linear source-count penalty). Every existing report measured a routing
formula that no longer exists. `docs/SEMANTIC_BENCHMARK.md`'s threshold
inventory also still listed `Settings.confidence_threshold`'s "Current
value" as the pre-Phase-22 `0.6`, when `app/shared/config/settings.py`'s
actual shipped default is `0.5` -- simple doc drift, now fixed in that
table alongside the staleness note. Net effect: there is currently zero
valid live-production evidence for `_SIGNAL_WEIGHTS`,
`_DENSE_SIMILARITY_FLOOR`/`_DENSE_SIMILARITY_CEILING`, or
`Settings.confidence_threshold` -- not "insufficient sample size," but
"no sample that measures the code as it exists today."

**Per the task's own instruction, no values were changed.** Changing
`_SIGNAL_WEIGHTS`, the floor/ceiling, or the threshold now, with the
evidence base described above, would be exactly the "looks reasonable"
tuning this priority exists to prevent. `Settings.confidence_threshold`
was already configurable via `Settings`/environment before this review
(`app/shared/config/settings.py`); that remains unchanged and is the
correct mechanism for a future evidence-based update.

**One characterization finding worth flagging for the next real
calibration pass, not acted on here**: computed (via the unchanged, real
`_normalize_top_similarity`/`_normalize_rerank_score`/`_weighted_score`
helpers, not by hand) what the current formula does when dense similarity
is strong but the cross-encoder reranker strongly disagrees -- e.g. raw
`top_similarity=0.65` (at the calibrated ceiling) with `rerank_score=-20.0`
(the reranker flatly rejecting the same top chunk) and a single source
still composes to `confidence_score≈0.562`, above the real `0.5` default
threshold, i.e. routes to `"answer"` despite the reranker's disagreement.
`top_similarity`'s weight (0.40, the largest of the four) and a single
source's own 0.70 floor on `_distinct_source_count_signal`'s curve are
enough on their own to outweigh a reranker score at the extreme low end of
its calibrated range. This is reported as a named scenario for a future
live calibration run to check against real ground truth
(`test_high_similarity_with_strongly_conflicting_rerank_signal` pins the
current numeric behavior, not a claim that it is safe) -- not treated as
grounds to reweight `_SIGNAL_WEIGHTS` unilaterally here, which would be
exactly the un-evidenced tuning this priority forbids.

**Added**: `tests/agents/test_confidence_routing_scenarios.py` (9 new
tests) -- clearly answerable/high-confidence -> answer;
clearly-unsupported -> investigation; borderline evidence (both signals at
their own documented "coin flip" anchors); high similarity with strongly
conflicting rerank (the named limitation above); low similarity despite
strong rerank + many sources; the inclusive `>=` boundary at the real
production default (not an arbitrary test threshold); a canary pinning
`Settings.confidence_threshold`'s shipped default itself (0.5); and two
regression guards for this review's own two findings (the fixture-vs-real-
formula gap, and the report-staleness gap). Every expected score is
computed independently through the same private helper functions
`evaluate_confidence` calls, never a hand-typed constant, so this file
breaks loudly if a future change to the weights/floor/ceiling/threshold
shifts any pinned outcome without updating this file's own evidence trail.

**Updated**: `docs/SEMANTIC_BENCHMARK.md`'s threshold inventory row for
`Settings.confidence_threshold` (current-value correction, staleness
note -- see Finding 2 above).

**Verified**: `tests/agents/test_confidence_routing_scenarios.py` (9/9
passed), `tests/core/auth/` + `tests/api/` (125/125 passed, unrelated to
this change -- last touched earlier this session), Tier 1
`scripts/run_evaluation.py` re-run clean (`VERDICT: CLEAN`, 56 cases,
unchanged). Full backend suite not re-run in this pass (this environment's
per-command timeout does not fit the full suite in one call); nothing in
`app/agents/confidence.py` itself was modified, so no broader regression
is expected, but a full `pytest` run is recommended before this is
considered fully re-verified.

**Runbook for completing production validation (unchanged prerequisite,
restated precisely)**:
```
python scripts/eval_confidence.py --report-path scripts/eval_confidence_report_<YYYY-MM-DD>.json
```
Requires: (1) a live Postgres database with the same kind of real,
already-ingested corpus `tests/rag_validation/rag_dataset.json` and
`scripts/eval_confidence_dataset.json` already assume (a `test-org`
organization with real Slack/GitHub-sourced documents -- 81 documents in
the last known-good corpus, per that dataset's own header comment); (2) a
funded `OPENAI_API_KEY` (real cost per run, `answer_question`'s real LLM
calls). Then: read the new report's `threshold_sweep` and
`confidence_by_category` sections, re-run
`app.evaluation.semantic.calibration.calibration_from_eval_confidence_report`
against it (via `scripts/run_semantic_evaluation.py` or directly) to check
whether n now clears the 20-example floor and whether `status` reaches
`"provisional"` or, across repeated runs, `"calibrated"` -- only then
change `Settings.confidence_threshold`'s default, citing that run's date
and numbers in the field's own comment, per that comment's existing
instruction. The same run's raw per-category confidence values are also
the first real opportunity to check `_SIGNAL_WEIGHTS` against ground
truth (bucket the real `confidence_signals` values `eval_confidence.py`
already records per question against `expected_route`, the same way this
session's fixture-derived n=9 bucket analysis works, just on real data).

**Deferred, unchanged from Phase 23/24**: no realistic production corpus
beyond the existing 36-question `test-org` golden set; `_SIGNAL_WEIGHTS`
remains a 4-value weighting scheme, not a single scalar
`app.evaluation.semantic.calibration.sweep_binary_threshold` can examine
directly (still `insufficient_data (n=0)` by that package's own
classification, unchanged); same-provider evaluator limitation; hosted CI
execution of the live-data jobs still unverified.

## Phase 28 (Multi-organization login/session design gap, this session)

**Root cause**: `core.auth.service._issue_access_token` puts exactly one
`organization_id` into an access token, and `core.auth.service.login_with_password`
resolved that single organization via `core.users.service.resolve_organization_for_login`
-> `core.users.repository.get_first_organization_id` -> `resolve_user_first_organization`
(a `LIMIT 1`-shaped SQL function, `c5e2a9f4d7b3`) -- a password-authenticated
user who belongs to more than one organization was silently logged into
whichever one that function happened to return first, with no way to
choose or later switch. `UserRole`'s `(user_id, organization_id, role_id)`
composite primary key already represents multi-organization membership at
the data layer; nothing above it could act on that.

**Fix, at a glance**: `login_with_password` now calls a new, additive
sibling (`core.users.service.list_organizations_for_login` ->
`core.users.repository.list_organization_ids` -> a new SECURITY DEFINER
`list_user_organization_ids` SQL function, migration `f6a7b8c9d0e1`) that
returns *every* organization the user belongs to, and branches on the
count: 0 -> unchanged `auth.no_organization` error; 1 -> unchanged
single-organization login; >1 -> a new `OrganizationSelectionRequired`
response instead of a silent pick, carrying a short-lived
`selection_token` and the candidate organizations. `resolve_organization_for_login`
and `get_first_organization_id` are both left completely unchanged (per
the task's explicit instruction) and are simply no longer called from
`login_with_password`.

**Current login behavior before this change**: `POST /auth/login` always
returned `SessionTokens` (an access + refresh token pair), for exactly one
organization, chosen by `get_first_organization_id`'s `LIMIT 1` query --
deterministic only in the sense that the same query plan tends to return
rows in the same order, never a documented or guaranteed ordering.

**New multi-organization login flow**:
  1. `POST /auth/login` (email/password) succeeds password verification as
     before.
  2. Backend resolves every organization the user is a member of
     (`list_organizations_for_login`).
  3. Exactly one organization -> `SessionTokens` (`status: "complete"`),
     byte-for-byte the same behavior a single-organization user always saw
     (plus the additive `status` field, which nothing existing reads).
  4. More than one -> `OrganizationSelectionRequired` (`status:
     "organization_selection_required"`): a `selection_token` (proves the
     password check already succeeded, 10-minute default expiry, configurable
     via `Settings.org_selection_token_expiry_minutes`) plus the list of
     candidate organizations (`id`/`name`/`slug` only).
  5. Client calls `POST /auth/select-organization` with that
     `selection_token` plus the chosen `organization_id`. The backend
     independently re-verifies membership (never trusts the request body's
     `organization_id` by itself) and, only then, issues real `SessionTokens`
     for that organization.
  6. Separately, an already-logged-in user can call
     `POST /auth/switch-organization` (bearer-token authenticated) to
     re-scope to a different organization they also belong to -- same
     independent membership re-verification, same fresh-token-issuance
     model. `GET /auth/organizations` lists the caller's own memberships for
     building a switcher UI.

**Endpoints added/changed**:
  - `POST /auth/login` -- response type changed from `SessionTokens` to the
    discriminated union `SessionTokens | OrganizationSelectionRequired`
    (`LoginResponse`, `Field(discriminator="status")`); existing
    single-organization behavior is unchanged.
  - `POST /auth/select-organization` (new) -- exchanges a `selection_token`
    + chosen `organization_id` for `SessionTokens`. Same per-IP rate limit
    as `/auth/login` (`_LOGIN_RATE_LIMIT`), since it is still an
    unauthenticated, credential-adjacent endpoint.
  - `POST /auth/switch-organization` (new, `CurrentIdentity`-gated) --
    re-scopes an authenticated session to a different member organization,
    returning fresh `SessionTokens`.
  - `GET /auth/organizations` (new, `CurrentIdentity`-gated) -- lists every
    organization the *caller* belongs to (`OrganizationsListResponse`),
    derived from the verified identity, never a client-supplied user id.

**JWT/session changes**:
  - `_issue_access_token` now sets an explicit `"type": "access"` claim.
    `verify_access_token` treats a *missing* `type` claim as `"access"`
    (backward-compatible with every access token issued before this
    change) but rejects any token whose `type` is something else.
  - A new, distinct token kind: the org-selection token (`_issue_org_selection_token`
    / `_verify_org_selection_token`, `"type": "org_selection"`), signed
    with the same `jwt_secret_key` but never accepted by `verify_access_token`
    or any `CurrentIdentity`-gated endpoint -- verified directly by unit
    test (see below).
  - Switching/selecting an organization always mints a brand-new access +
    refresh token pair (`_issue_session`, fresh `family_id`) for the target
    organization rather than mutating any existing token or session in
    place; `_issue_session` itself already calls `set_tenant_context` for
    the target organization before writing the new refresh token row, so
    the new session is correctly tenant-scoped from the moment it's created.
    The prior session (if any) is left exactly as it was -- still valid
    until its own expiry/logout, but it does not gain access to the newly
    selected organization.

**Security checks performed**:
  - `select_organization` and `switch_organization` both independently
    call `list_organizations_for_login` and reject (`auth.not_a_member`)
    if the requested `organization_id` is not in that list -- a client
    naming a real organization it does not belong to is rejected exactly
    like naming a nonexistent one (test: "cannot access Organization B data
    merely by manipulating the requested organization ID").
  - `switch_organization`'s `user_id` comes only from the router's
    `CurrentIdentity` dependency (the caller's own verified access token),
    never from the request body.
  - Verified end to end (not just by construction) that an org-selection
    token is rejected by `verify_access_token`, that an access token is
    rejected by `_verify_org_selection_token`, and that a real,
    pre-existing-shape access token (no `type` claim, as every token
    issued before this change looked) still verifies successfully --
    exercised directly against the real JWT sign/verify code path, not
    mocked.
  - `get_organizations_by_ids` returns `[]` (never "every organization")
    for an empty id list, so a zero-organization user's
    `GET /auth/organizations` can never accidentally leak the full
    organization table.
  - No RLS/tenant-isolation policy was touched. `list_user_organization_ids`
    (the new SQL function) is a narrow, read-only, `SECURITY DEFINER`
    enumeration of exactly the same shape as the existing
    `resolve_user_first_organization` -- "which organizations does this
    user_id hold a role in" -- generalized from `LIMIT 1` to no limit;
    `organizations` itself carries no RLS policy (confirmed by inspection:
    it IS the tenant boundary, not something a policy scopes), so reading
    it by a caller-verified id list needs no `set_tenant_context` call, the
    same reasoning `get_organization_by_id` already relied on.

**Tests added** (`tests/core/auth/test_multi_organization_login.py`, 14
tests; `tests/core/users/test_multi_org_repo_additions.py`, 5 tests) --
covering, by the task's own numbering:
  1. Single-org user logs in normally (`status: "complete"`, one
     `_issue_session` call for that organization).
  2. Multi-org user gets `OrganizationSelectionRequired`, never a silently
     issued token.
  3. `list_available_organizations` returns only the caller's own
     organizations.
  4. Both `switch_organization` and `select_organization` reject a
     non-member organization id.
  5/6. A successful switch issues a token for the *selected* organization
     specifically (verified via `_issue_session`'s recorded call args and,
     separately, a real `_issue_access_token`/`verify_access_token` round
     trip).
  7/8. Full existing suite re-run (below) -- no regressions.
  9. Explicitly restated "attacker" framing: a real organization id the
     caller does not belong to is rejected exactly like scenario 4.
  10. Zero-organization users: login rejected safely
     (`auth.no_organization`, unchanged), switch rejected
     (`auth.not_a_member`), organizations listing returns empty rather than
     erroring.
  Plus: token-type isolation in both directions, and the pre-existing
  (no-`type`-claim) access token backward-compatibility case.

**Full test results**: `tests/core/auth/test_multi_organization_login.py`
14/14 passed; `tests/core/users/test_multi_org_repo_additions.py` 5/5
passed. Full existing suite re-run across
`tests/{core,api,agents,database,shared,ingestion,ingestion_retrieval,retrieval,mcp}`:
904 passed, 2 failed / 5 errors -- all five errors and both failures are
`tests/shared/security/test_azure_kms.py`'s pre-existing `ModuleNotFoundError:
No module named 'azure'` (the `azure-*` SDK packages are not installed in
this environment; unrelated to this change, confirmed by re-reading that
file: it tests `core.tenancy`'s Azure Key Vault KMS provider, nothing this
task touched). No test outside that one file failed. Migration head
verified via the same regex-based script used earlier this session: 22
total migrations, single head `f6a7b8c9d0e1`. OpenAPI schema generation for
`/auth/login` verified directly: a proper `oneOf` with a `discriminator`
mapping `"complete"` -> `SessionTokens` and
`"organization_selection_required"` -> `OrganizationSelectionRequired`.

**Frontend work**: not yet done this pass -- see "Any remaining
limitations" below; `frontend/src/context/TenantContext.tsx`'s own Phase
7.8 docstring already anticipated exactly this backend capability
("build this once it does, not before") and is the natural next place to
wire a real organization switcher once the frontend changes are made.

**Any remaining limitations**:
  - Frontend (`types/auth.ts`, `api/auth.ts`, `AuthContext.tsx`, a
    login-time organization-selection UI, `TenantContext.tsx`/
    `TenantSwitcher.tsx`) has not yet been updated to consume the new
    `OrganizationSelectionRequired` login response or the
    select/switch/list endpoints -- today's frontend `login()` still
    expects a bare `SessionTokens` and would need to branch on `status`
    the same way the backend now does.
  - `select_organization`/`switch_organization` currently accept any
    request whose membership check passes with no additional rate
    limiting beyond `/auth/select-organization` sharing `/auth/login`'s
    per-IP limit; `/auth/switch-organization` (bearer-token gated) has no
    dedicated rate limit of its own, matching every other
    `CurrentIdentity`-gated endpoint in this router today.
  - No UI/API surfaces an organization's `name`/`slug` beyond the minimal
    `OrganizationSummary` shape -- sufficient for a switcher, not a full
    organization-management view.
  - This still assumes password-authenticated multi-organization
    membership arises only via some out-of-band mechanism (invitations,
    admin action) -- `signup` itself still always creates exactly one new
    organization per account, unchanged.


## Phase 29 (Production incident: incidents unable to load / create, this session)

**Symptom reported**: "new incident failed to create and also incidents are
unable to load" against the deployed frontend
(`https://frontend-production-ded3.up.railway.app/incidents/new`).

**Diagnosis**: live browser automation against the deployed frontend showed
the incidents list stuck on "Loading…" and then "Something went wrong -- We
couldn't load this data." Instrumenting `window.fetch` on the live page
captured the actual failing request: `GET /incidents?limit=20&offset=0` ->
`403 {"error_code": "permission_denied", "message": "You do not have
permission to perform this action.", "detail": {"required_permission":
"incident:read"}}`. Session/token machinery itself was confirmed healthy
first (a direct `POST /auth/refresh` with the browser's stored refresh
token returned a fresh, valid access token) -- this ruled out Phase 28's
token-type-claim change before looking further, and pointed squarely at an
authorization/RBAC gap rather than an authentication one.

**Root cause**: `app.database.migrations.versions.d706a360fc2a`
(2026-08-18) added the `incident:read` permission, gated
`core.incidents.service.get_incident`/`list_incidents`/`get_timeline` on it,
and backfilled every role that existed *at that migration's run time* -- by
its own explicit design, it does not affect roles created afterward.
`core.users.repository.ADMIN_PERMISSION_CODES` -- the fixed list
`core.users.service.ensure_admin_role` grants to the single shared "admin"
role on every self-service signup -- was never updated to include
`incident:read` (only `scripts/seed_test_organization.py`'s dev-only
bootstrap was, per its own "one exception" comment, which named exactly
this risk without it being carried through to the production list). Every
organization whose admin role was created or re-granted by real signup
after `d706a360fc2a` shipped could therefore write incidents
(`incident:write`, which the list does have) but not read them back --
the exact 403 observed.

**Fix**:
  - `app/core/users/repository.py` -- added `"incident:read"` to
    `ADMIN_PERMISSION_CODES`, with a comment explaining the drift so it
    doesn't recur silently. `ensure_admin_role` is idempotent and re-grants
    the full list to the same shared "admin" role on every signup, so this
    alone self-heals every affected organization the next time anyone signs
    up anywhere.
  - New migration `f3e7c05b146e_grant_incident_read_to_admin_role.py`
    (`Revises: f6a7b8c9d0e1`, the Phase 28 head) grants `incident:read` to
    the `"admin"` role immediately, via the same idempotent
    `INSERT ... ON CONFLICT DO NOTHING` pattern `d706a360fc2a` used, scoped
    to `r.name = 'admin'` specifically (the only role
    `get_or_create_role_by_name` is ever called with -- confirmed by
    grepping every call site) rather than every role, since `d706a360fc2a`
    already covered every role that existed before it.
  - New regression test
    (`tests/core/users/test_admin_permission_codes.py`, 3 tests) pins
    `incident:read` (and `incident:write`) into `ADMIN_PERMISSION_CODES`
    directly against `core.incidents.service`'s own permission constants,
    plus a duplicate-entry guard -- this class of gap is now caught without
    a live database or a real signup.

**Test results**: new test file 3/3 passed. Full re-run of
`tests/{core,api,database,shared,ingestion,ingestion_retrieval,retrieval,mcp,agents}`:
435 passed in the first, focused pass
(`tests/{core,api,database}`); the remaining suites added 472 passed / 2
failed / 5 errors, all seven exclusively
`tests/shared/security/test_azure_kms.py`'s pre-existing
`ModuleNotFoundError: No module named 'azure'` (azure-\* SDK not installed
in this environment -- unrelated to this change, identical to every prior
session's baseline). No test outside that one file failed. Migration head
verified via the same regex-based script used throughout this session: 23
total migrations, single head `f3e7c05b146e`.

**Remaining limitation / action needed**: the migration has been written
and verified (syntax, single-head, idempotency by inspection) but **has not
been applied to the production database** -- this session has no
production `DATABASE_URL` credentials, and applying it is a real write
against live data, which this session's standing instructions require
surfacing before doing. Applying
`f3e7c05b146e_grant_incident_read_to_admin_role.py` (`alembic upgrade
head`) against production is the one remaining step to restore incident
reads for every organization affected today without waiting for a new
signup to self-heal it.

## Important context to continue

- Never fabricate test/verification results. Distinguish PASS (actually run, actually passed) / PARTIAL / BLOCKED (environment-limited) / FAILED honestly, always.
- Never modify Neon or apply pending migrations without the user's explicit, in-the-moment authorization — prior batch-level "yes" confirmations do not carry forward to new destructive actions.
- Never invent a backend endpoint, schema field, or permission to make a frontend feature look complete — if the backend capability doesn't exist, document the gap instead of building a UI that will always fail.
- Backend authorization is authoritative; frontend permission checks are UX-only convenience, never the real gate.
- Module boundaries are enforced by `import-linter` (`uv run lint-imports`) — `app.api`/`app.core` may never import `app.ingestion`/`app.mcp` internals.
