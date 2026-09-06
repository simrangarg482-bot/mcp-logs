"""Application configuration, loaded from environment variables.

This is the first implementation file in the project, deliberately: almost
every other module (database connections, Redis, LLM API keys, MCP auth)
depends on settings being loaded correctly, so it has to exist before
anything else can be written meaningfully.

Owned by: shared/ (ARCHITECTURE.md section 3 -- cross-cutting, no business
meaning of its own, importable by every other module).
"""

from functools import lru_cache
from typing import Annotated, ClassVar, Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for runtime configuration.

    Every field here corresponds to a concrete need from a document we've
    already written:
      - database_url          -> Neon Postgres connection (DATABASE_DESIGN.md)
      - redis_url              -> ingestion job queue (ENGINEERING_DECISIONS.md #002)
      - default_vector_backend -> per-collection choice exists in
                                   ARCHITECTURE.md section 8, but the
                                   *default* backend for new collections is
                                   a global setting
      - openai_api_key      -> LLM calls in agents/ (AGENT_WORKFLOWS.md)
      - confidence_threshold   -> the routing threshold in the Confidence
                                   Evaluation Node (AGENT_WORKFLOWS.md 2.2)
                                   -- exposed as config, not hardcoded, since
                                   the exact value is still an open item in
                                   ENGINEERING_DECISIONS.md 

    Deliberately NOT included: individual connector credentials (Slack/GitHub/
    Jira tokens). Those belong to ingestion/connectors/ configuration, scoped
    per-source, not global app settings -- mixing them in here would make
    this class a dumping ground and couple core app startup to whichever
    connectors happen to be configured.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment -----------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Tracing (Phase 5.3, app.shared.config.tracing) --------------------
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        description=(
            "OTLP collector endpoint (e.g. 'http://localhost:4317') spans "
            "are exported to. No real collector is deployed for this "
            "project yet -- leave unset (the default) to create spans "
            "without exporting them."
        ),
    )
    otel_console_exporter_enabled: bool = Field(
        default=False,
        description=(
            "Print complete spans to stdout when no OTLP collector is configured. "
            "Disabled by default because per-document ingestion spans are verbose."
        ),
    )

    # --- Database (DATABASE_DESIGN.md) ------------------------------------
    database_url: PostgresDsn = Field(
        description=(
            "Application runtime connection, asyncpg driver. Must be the "
            "least-privileged `ekip_app` role (NOSUPERUSER/NOBYPASSRLS -- "
            "see migration b8f3d6a1c4e7 and "
            "EKIP_TENANT_ISOLATION_SECURITY_REVIEW.md recommendation #2), "
            "never an admin/superuser role: this is the connection every "
            "RLS policy from c7d4e8f19a2b is enforced against, and a role "
            "with BYPASSRLS (e.g. Neon's neondb_owner, Railway's default "
            "Postgres user) makes every one of those policies a silent "
            "no-op."
        )
    )
    database_echo: bool = Field(
        default=False,
        description="Emit every SQL statement. Enable only for focused database debugging.",
    )
    migration_database_url: PostgresDsn | None = Field(
        default=None,
        description=(
            "Admin/superuser connection used ONLY by Alembic "
            "(app/database/migrations/base.py), for topologies where the "
            "migration step and the running application share one process's "
            "environment (e.g. Railway's `preDeployCommand`, which runs in "
            "the same service -- and therefore the same env vars -- as the "
            "app itself). Falls back to `database_url` when unset, which is "
            "correct and unchanged for docker-compose (its `migrate` "
            "service already overrides DATABASE_URL to a separate admin "
            "credential at the container level) and infra/main.bicep (whose "
            "`migrateJob` already uses a distinct admin credential the same "
            "way) -- see docs/operations/deployment.md's 'Migration database "
            "vs runtime database'. `ekip_app` itself is deliberately never "
            "granted the CREATE ROLE / ALTER TABLE / GRANT privileges "
            "migrations need, so alembic upgrade head cannot run as "
            "`database_url`'s role in a topology where that role has been "
            "correctly narrowed to `ekip_app`."
        ),
    )

    # --- Job queue (ENGINEERING_DECISIONS.md #002) ------------------------
    redis_url: RedisDsn = Field(
        description="Backs the arq job queue used by ingestion workers."
    )

    # --- Vector retrieval (ARCHITECTURE.md section 8) ----------------------
    default_vector_backend: Literal["pgvector", "qdrant"] = "qdrant"
    qdrant_url: str | None = Field(
        default=None,
        description="Required only if any collection uses the qdrant backend.",
    )

    # --- LLM (AGENT_WORKFLOWS.md; ENGINEERING_DECISIONS.md #008) -----------
    openai_api_key: str = Field(description="Used by all agent LLM calls.")
    agent_llm_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "OpenAI chat model used by every LLM-calling agent node (query "
            "rewriting, Answer Agent generation, hypothesis generation). A "
            "single global setting, not one per node -- no node in "
            "AGENT_WORKFLOWS.md has a documented reason to use a different "
            "model than any other yet; split this into per-node settings if "
            "one ever does."
        ),
    )

    # --- Agent behavior (AGENT_WORKFLOWS.md 2.2) ---------------------------
    # `default=0.5` as of 2026-09-02 (was 0.6). PROVISIONAL -- a projection,
    # not a fresh sweep. Background:
    #
    # The 2026-08-13 `scripts/eval_confidence.py` run against `test-org`'s
    # live corpus (36 questions: 14 clear-answer / 12 ambiguous / 10
    # no-information; scripts/eval_confidence_report.json) measured the OLD
    # confidence signals. It found:
    #   - Sweeping 0.40-0.80, 0.40 scored marginally higher on F1 (0.700 vs
    #     0.6's 0.667) -- a 0.033 margin on 36 questions, within one question
    #     flipping category by chance, so not acted on at the time.
    #   - 2 of 14 clear-answer questions misrouted to investigation at 0.6
    #     (confidence 0.422 and 0.454) -- both single-source questions.
    #   - clear-answer (0.422-1.000) and ambiguous (0.572-1.000) confidence
    #     ranges overlapped: `top_similarity`/`rerank_score` measured topical
    #     relevance, not whether the specific fact asked was present. No
    #     threshold separates those two -- a signal-quality gap, not a
    #     threshold-tuning one.
    #   - no-information clustered tightly at ~0.389, well clear of the gate.
    #
    # `app/agents/confidence.py` was then changed (EKIP audit findings 1
    # and 2): `source_count` no longer scores a single-source answer at 0.2
    # (diminishing-returns curve, 1 source -> 0.70), and `top_similarity` is
    # now the top dense cosine similarity, not a near-constant ~0.5 fused-RRF
    # artifact. Working the weighted formula through with those signals and
    # the 2026-08-13 per-category behavior projects: no-information ~0.17
    # (far below any sane gate), the two former misroutes back above ~0.55,
    # clear-answer strong ~0.85, ambiguous still ~0.80 and still overlapping
    # clear-answer (that signal-quality gap is unchanged). 0.5 sits below
    # the weakest projected genuine clear-answer and well above
    # no-information; going lower mostly just raises the
    # confidently-wrong-answer (false-positive) rate without rescuing more
    # clear-answer questions. Exact per-question values depend on
    # cross-encoder logits this projection had to estimate -- hence
    # provisional.
    #
    # CONFIRM THIS: re-run
    # `scripts/eval_confidence.py --compare-to scripts/eval_confidence_report.json`
    # against live `test-org` data, check its per-category regression output,
    # set this from that sweep, and replace this comment with the run's date
    # and findings. scripts/eval_confidence_report.json is stale until then.
    #
    # RE-CONFIRMED, 2026-09-05 calibration review (docs/PROJECT_STATUS.md
    # Phase 27): still not empirically calibrated. Live evaluation remains
    # blocked in every environment tried so far (no live test-org database +
    # funded OPENAI_API_KEY available) -- this review did not fabricate a
    # number to fill that gap. Also confirmed: every existing checked-in
    # scripts/eval_confidence_report*.json predates BOTH the 2026-08-30
    # reranker calibration and the 2026-09-02 signal-shape rewrite
    # (app/agents/confidence.py's own module docstring) -- those reports
    # measured a since-replaced scoring formula, so they are not valid
    # evidence for this default even setting aside their small sample size
    # (n=14, below app.evaluation.semantic.calibration's 20-example floor).
    # This value (0.5) is an honest placeholder, not a validated one.
    confidence_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
    )

    # --- AI cost budget enforcement (Phase 6.6, app.agents.cost_budget) ----
    max_organization_cost_usd_per_day: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "If set, the maximum estimated LLM spend (app.agents.telemetry."
            "get_estimated_cost_usd's pricing table, not real OpenAI billing "
            "data) one organization may accumulate across a rolling 24-hour "
            "window before answer_question/triage_incident/generate_postmortem/"
            "detect_knowledge_gaps refuse to make a further LLM call for that "
            "organization (CostBudgetExceededError, 429). Left unset (the "
            "default) means no enforcement -- this codebase does not invent "
            "a 'reasonable' default dollar cap for a deployment it knows "
            "nothing about; an operator who wants this protection sets an "
            "explicit value for their own actual usage/budget expectations."
        ),
    )

    # --- Agent memory (Priority 4, app.core.memory) -------------------------
    # Every one of these three is a real bound on how much persistent memory
    # can influence an answer. Memory grows without limit over time, so
    # "inject whatever is relevant" is not a safe default at any horizon --
    # these caps are what keep a years-old memory store from crowding out the
    # freshly-retrieved evidence an answer is actually supposed to be
    # grounded in. Defaults are deliberately conservative; see
    # docs/AGENT_MEMORY.md for how they interact.
    memory_recall_limit: int = Field(
        default=5,
        ge=0,
        le=50,
        description=(
            "Maximum number of memories injected into one agent context. "
            "5 is small on purpose: memory is supplementary context, not the "
            "evidence base, and the answer path already receives up to 20 "
            "reranked document chunks. Setting 0 disables memory injection "
            "entirely without needing a separate feature flag."
        ),
    )
    memory_relevance_threshold: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum cosine relevance (1 - cosine_distance) for a recalled "
            "memory to be injected. A placeholder, honestly labelled: like "
            "confidence_threshold's current default, this has NOT been "
            "directly swept against real data, because doing so needs a real "
            "memory corpus that does not exist "
            "yet. 0.35 is set low enough to admit genuine topical matches "
            "and high enough to exclude the unrelated -- verified only "
            "against the deterministic fixtures in tests/core/memory/, not "
            "against production data. Raise it if irrelevant memory starts "
            "reaching answers."
        ),
    )
    memory_context_char_budget: int = Field(
        default=2000,
        ge=0,
        description=(
            "Total characters of memory text injectable into one context, "
            "enforced after the per-memory limit and the recall limit. Sized "
            "against app.agents.retrieval.context_assembly's own 4000-token "
            "(~16000-character) evidence budget so memory can occupy at most "
            "roughly an eighth of the assembled context -- memory should "
            "inform an answer, never dominate it."
        ),
    )

    # --- Auth (API_DESIGN.md section 1) -------------------------------------
    jwt_secret_key: str = Field(description="Signs/verifies session tokens.")
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    REFRESH_TOKEN_EXPIRY_DAYS: ClassVar[int] = 30
    org_selection_token_expiry_minutes: int = Field(
        default=10,
        ge=1,
        description=(
            "How long a multi-organization login's `selection_token` "
            "(core.auth.schemas.OrganizationSelectionRequired) stays valid "
            "before `POST /auth/select-organization` must be called. Signed "
            "with the same jwt_secret_key but a distinct token `type` claim "
            "(see core.auth.service.verify_access_token's type check) so it "
            "can never be presented as, or accidentally accepted as, a real "
            "access token -- deliberately short-lived since it exists only "
            "to bridge the few seconds between a successful password check "
            "and the user picking which of their organizations to log into."
        ),
    )

    # --- MCP server (scripts/run_mcp_server.py) -----------------------------
    mcp_port: int = Field(
        default=8001,
        description=(
            "Local TCP port `scripts/run_mcp_server.py` binds the streamable-"
            "HTTP MCP transport to. Override via MCP_PORT -- e.g. if 8001 is "
            "already in use (a stale server process from a previous run is "
            "the usual cause; check `netstat`/`Get-NetTCPConnection` before "
            "assuming a real conflict) or you deliberately want a different "
            "port. `scripts/live_mcp_tests/conftest.py`'s default MCP URL and "
            "this module's `mcp_public_base_url` default both derive from "
            "this value, so changing it here keeps them in sync -- but if "
            "you front this server with ngrok, its LOCAL target "
            "(`ngrok http <port>`) must still be updated to match by hand; "
            "nothing here can reach into your ngrok config."
        ),
    )

    # --- MCP OAuth bridge (app/mcp/oauth) -- Claude's remote-connector OAuth
    # flow needs a real, publicly-reachable HTTPS base URL to advertise as its
    # `issuer_url`/`resource_server_url` (OAuth server/resource metadata is
    # discovered from this URL) -- it cannot be `localhost`, since Claude
    # connects from Anthropic's cloud, not the machine running this server.
    mcp_public_base_url: str = Field(
        default="http://localhost:8001",
        description=(
            "Public HTTPS base URL this MCP server is reachable at (e.g. the "
            "ngrok URL fronting it) -- used as the OAuth issuer_url/"
            "resource_server_url so Claude's remote-connector OAuth flow can "
            "discover this server's /authorize, /token, and registration "
            "endpoints. Override via MCP_PUBLIC_BASE_URL; the localhost "
            "default only works for same-machine MCP clients, not Claude. "
            "If MCP_PUBLIC_BASE_URL is left unset, its port is kept in sync "
            "with `mcp_port` automatically (see `_sync_local_public_base_url_port`) "
            "-- an explicit override (e.g. a real ngrok hostname, which has "
            "no port of its own) always wins outright."
        ),
    )

    # --- CORS (browser-based frontends, e.g. frontend/) ---------------------
    # `NoDecode` is required here: pydantic-settings' own env source attempts
    # to JSON-decode any list-typed field's raw env value *before* the
    # `_split_cors_origins` validator below ever runs, so a real
    # `CORS_ALLOWED_ORIGINS=http://a,http://b` env var crashed the app
    # outright at startup (`SettingsError` from a failed `json.loads` on a
    # non-JSON string) despite this field's own description promising
    # comma-separated support -- caught by an actual browser E2E run
    # (frontend/e2e/), not by any unit test, since nothing exercises real
    # process startup with a real env var override. `NoDecode` tells
    # pydantic-settings to hand the raw string straight to the validator
    # instead of pre-decoding it as JSON.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        description=(
            "Origins allowed to call this API from a browser. Defaults to "
            "the EKIP frontend's Vite dev server; override via the "
            "CORS_ALLOWED_ORIGINS env var (comma-separated) for any other "
            "deployed frontend origin."
        ),
    )

    # --- Investigation Agent live evidence (AGENT_WORKFLOWS.md 2.4's hybrid
    # evidence-gathering extension -- agents/investigation/live/) -----------
    investigation_live_evidence_enabled: bool = Field(
        default=True,
        description=(
            "Global kill-switch for the Investigation Agent's live GitHub/"
            "Slack lookups (agents/investigation/live/). Set False to fall "
            "back to indexed-only evidence gathering without a code change "
            "-- e.g. if live external API calls start tripping rate limits "
            "or add unacceptable latency in production."
        ),
    )
    investigation_live_evidence_lookback_hours: int = Field(
        default=24,
        ge=1,
        description=(
            "How far back (from now) a live evidence source searches for "
            "recent commits/PRs/issues/messages, independent of the "
            "hourly ingestion reconciliation cadence (app.ingestion.workers."
            "main.scheduled_reconciliation) -- live evidence's job is "
            "covering the gap between the last sync and right now, not "
            "re-walking a source's whole history."
        ),
    )

    # --- Investigation Agent critique (Priority 7, agents/investigation/
    # critique.py) -- the bounded pass/revision ceiling itself
    # (MAX_CRITIQUE_PASSES/MAX_REVISION_ATTEMPTS) is a code constant, NOT a
    # setting, specifically so no configuration change can silently widen it
    # into an unbounded loop. Only the softer behavioral thresholds below
    # are configurable. -----------------------------------------------------
    investigation_critique_enabled: bool = Field(
        default=True,
        description=(
            "Global kill-switch for the bounded critique/reflection pass "
            "over Investigation Agent hypotheses. Set False to skip "
            "critique entirely -- hypotheses are then persisted with "
            "review_status='not_reviewed', exactly as before this feature "
            "existed, e.g. if critique LLM calls start tripping rate "
            "limits or adding unacceptable latency in production."
        ),
    )
    investigation_critique_min_evidence_count: int = Field(
        default=2,
        ge=0,
        description=(
            "Below this many gathered EvidenceItems, structural validation "
            "flags the whole investigation 'insufficient_information' and "
            "rejects it without spending a critique LLM call -- a "
            "deterministic pre-check for an objectively thin evidence base."
        ),
    )
    investigation_critique_overconfidence_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description=(
            "A hypothesis whose self-reported `confidence` is at or above "
            "this value while citing fewer than "
            "investigation_critique_min_evidence_per_hypothesis pieces of "
            "evidence is flagged 'overconfidence' by structural validation. "
            "An initial, uncalibrated value -- see docs/"
            "INVESTIGATION_CRITIQUE.md's limitations section."
        ),
    )
    investigation_critique_min_evidence_per_hypothesis: int = Field(
        default=2,
        ge=1,
        description=(
            "Paired with investigation_critique_overconfidence_threshold: "
            "how many supporting_evidence_ids a high-confidence hypothesis "
            "must cite before it is NOT flagged as overconfident."
        ),
    )

    # --- Knowledge Gap Agent (AGENT_WORKFLOWS.md 2.6 / PROJECT_PLAN.md 6.6,
    # app/agents/knowledge_gap/) ---------------------------------------------
    knowledge_gap_lookback_days: int = Field(
        default=14,
        ge=1,
        description=(
            "How far back the Knowledge Gap Agent looks for low-confidence "
            "`answer_question` executions when clustering for repeated gaps."
        ),
    )
    knowledge_gap_min_cluster_size: int = Field(
        default=3,
        ge=2,
        description=(
            "A cluster of similar low-confidence queries must reach this "
            "size before it's surfaced as a `GapReport` -- distinguishes a "
            "genuinely repeated gap from a one-off hard question "
            "(AGENT_WORKFLOWS.md: 'repeated gaps rather than one-off "
            "low-confidence queries')."
        ),
    )
    knowledge_gap_similarity_threshold: float = Field(
        default=0.82,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine-similarity threshold for joining a query to an existing "
            "cluster (app.agents.knowledge_gap.clustering.cluster_by_"
            "similarity) -- resolves AGENT_WORKFLOWS.md's previously-open "
            "'clustering method/threshold ... not yet decided' item in "
            "favor of similarity-threshold (leader) clustering over k-means; "
            "see that module's docstring for the full reasoning."
        ),
    )

    # --- Ingestion rate limiting (PROJECT_PLAN.md sections 4.5/10,
    # app/shared/rate_limiter.py) ------------------------------------------
    ingestion_org_max_requests_per_second: float = Field(
        default=5.0,
        gt=0.0,
        description=(
            "Aggregate requests/second budget shared across every "
            "connector_config belonging to one organization -- the 'per "
            "tenant' half of section 4.5's 'per connector, per tenant' "
            "rate-limiting requirement. Independent of (and in addition to) "
            "each individual connector's own declared `requests_per_second` "
            "ceiling; see `app.shared.rate_limiter`'s module docstring."
        ),
    )
    ingestion_job_timeout_seconds: int = Field(
        default=7200,
        ge=300,
        le=86400,
        description=(
            "Hard ARQ safety ceiling for one ingestion attempt. The two-hour "
            "default accommodates measured first GitHub syncs while progress is "
            "checkpointed page-by-page, so reaching this ceiling no longer "
            "forces the next attempt to restart the remote traversal."
        ),
    )
    ingestion_worker_max_jobs: int = Field(
        default=2,
        ge=1,
        le=32,
        description=(
            "Maximum ingestion jobs executed concurrently by one worker "
            "process. The default is deliberately below ARQ's default of 10: "
            "embedding is CPU- and memory-intensive, and oversubscription "
            "turns healthy work into cascading timeouts."
        ),
    )
    ingestion_checkpoint_ttl_seconds: int = Field(
        default=86400,
        ge=300,
        le=604800,
        description=(
            "Maximum age of a durable connector pagination checkpoint. Old "
            "or configuration-mismatched checkpoints are ignored safely."
        ),
    )
    ingestion_max_pages_per_attempt: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
        description="Fails a connector that paginates without a reasonable bound.",
    )
    ingestion_max_items_per_page: int = Field(
        default=2_000,
        ge=1,
        le=100_000,
        description="Maximum source items accepted from one connector page.",
    )
    ingestion_max_document_bytes: int = Field(
        default=10_000_000,
        ge=1_024,
        le=100_000_000,
        description="Maximum UTF-8 size of one normalized source document.",
    )
    ingestion_max_chunks_per_document: int = Field(
        default=1_000,
        ge=1,
        le=100_000,
        description="Maximum chunks one source document may generate.",
    )
    embedding_worker_threads: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Dedicated sentence-transformer executor size. This isolates "
            "embedding from the event loop's general-purpose thread pool."
        ),
    )
    embedding_batch_size: int = Field(
        default=32,
        ge=1,
        le=512,
        description="Sentence-transformer encode batch size.",
    )
    agent_reranking_enabled: bool = Field(
        default=True,
        description=(
            "Load the cross-encoder reranker (a second ~80MB transformer, on "
            "top of the embedding model) in the Retrieval Agent. Reranking is "
            "precision refinement over an already recall-complete candidate "
            "set (PROJECT_PLAN.md section 5.3); disabling it falls back to the "
            "RRF-fused order. Set to false for a memory-constrained process "
            "(e.g. co-locating the API server, both workers and the MCP "
            "server on one small host) where loading the second model tips "
            "the process into an unrecoverable native allocation failure."
        ),
    )

    # --- Secret management (PROJECT_PLAN.md section 12.5, Milestone 10;
    # Azure Key Vault provider added Phase 3) --------------------------------
    kms_provider: Literal["local", "azure"] = Field(
        default="local",
        description=(
            "Which app.shared.security.kms.KeyManagementService implementation "
            "get_kms() constructs. 'local' (the default) is development/test "
            "only -- see LocalKeyManagementService's docstring for exactly what "
            "security property it lacks versus a real KMS. 'azure' uses Azure "
            "Key Vault (AzureKeyVaultKeyManagementService) and requires "
            "azure_key_vault_url/azure_key_vault_key_name to also be set. "
            "Refused outright when environment=production and this is still "
            "'local' -- see _reject_local_kms_in_production below."
        ),
    )
    azure_key_vault_url: str | None = Field(
        default=None,
        description=(
            "e.g. 'https://ekip-prod.vault.azure.net' -- required when "
            "kms_provider=azure. Not a secret itself (it's a vault's public "
            "DNS name), so it's fine as a plain env var/App Service setting, "
            "unlike anything that would actually authenticate against it."
        ),
    )
    azure_key_vault_key_name: str | None = Field(
        default=None,
        description=(
            "Name of the RSA key inside the vault used to wrap/unwrap every "
            "connector-credential and SSO-client-secret DEK -- required when "
            "kms_provider=azure. Not the key material itself, just which named "
            "key to ask Key Vault to use; rotating it to a new *version* (same "
            "name) needs no config change at all, see kms.py's module docstring."
        ),
    )
    connector_secret_master_key: str | None = Field(
        default=None,
        description=(
            "Hex-encoded 32-byte AES key -- the key-encryption-key (KEK) "
            "`app.shared.security.kms.LocalKeyManagementService` uses to wrap "
            "each per-secret data-encryption-key (DEK). Required only when "
            "kms_provider=local (the default); ignored/unnecessary entirely "
            "when kms_provider=azure. This is a platform secret (like "
            "jwt_secret_key above), injected from the environment, never "
            "committed -- it is NOT a per-tenant connector credential itself."
        ),
    )

    @model_validator(mode="after")
    def _require_provider_specific_kms_settings(self) -> "Settings":
        """Each `kms_provider` value needs its own, different settings
        populated -- checked here, once, rather than letting a misconfigured
        deployment discover the gap the first time `get_kms()` (or an Azure
        SDK call inside it) fails at request time.
        """
        if self.kms_provider == "azure":
            missing = [
                name
                for name, value in (
                    ("azure_key_vault_url", self.azure_key_vault_url),
                    ("azure_key_vault_key_name", self.azure_key_vault_key_name),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"kms_provider=azure requires {', '.join(missing)} to also be set."
                )
        elif not self.connector_secret_master_key:
            raise ValueError(
                "kms_provider=local requires connector_secret_master_key to be set."
            )
        return self

    @model_validator(mode="after")
    def _reject_local_kms_in_production(self) -> "Settings":
        """PROJECT_PLAN.md's explicit requirement: production must never
        silently run with the development/test-only local KMS stand-in.
        Failing here, at settings-construction time (i.e. at process
        startup), means a misconfigured production deployment never even
        finishes booting -- not a runtime surprise the first time a
        connector is registered.
        """
        if self.environment == "production" and self.kms_provider == "local":
            raise ValueError(
                "kms_provider=local is not permitted when environment=production. "
                "Set KMS_PROVIDER=azure and its required azure_key_vault_* settings."
            )
        return self

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Accepts a comma-separated CORS_ALLOWED_ORIGINS env var string, not
        just a JSON array -- pydantic-settings only auto-parses list-typed
        env vars as JSON, which is an awkward way to set a simple origin list
        in a .env file or shell export.
        """
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_allowed_origins", mode="after")
    @classmethod
    def _reject_wildcard_origin(cls, value: list[str]) -> list[str]:
        """`app.api.main.create_app` hardcodes `allow_credentials=True` on
        `CORSMiddleware` -- Starlette's actual behavior for
        `allow_origins=["*"]` combined with `allow_credentials=True` is to
        echo the request's real `Origin` header back verbatim (browsers
        forbid a literal `*` `Access-Control-Allow-Origin` alongside
        credentials, so Starlette works around that by reflecting whatever
        origin asked), which defeats the allowlist entirely -- any site can
        then make a credentialed, cookie/token-bearing request. Failing fast
        at settings-load time is safer than only catching this in a security
        review of the deployed config.
        """
        if "*" in value:
            raise ValueError(
                "cors_allowed_origins must not contain '*' -- combined with "
                "this app's allow_credentials=True, a wildcard origin lets "
                "Starlette reflect any requesting origin back as allowed, "
                "which defeats the allowlist. List explicit origins instead."
            )
        return value

    _DEFAULT_LOCAL_PUBLIC_BASE_URL: ClassVar[str] = "http://localhost:8001"

    @model_validator(mode="after")
    def _sync_local_public_base_url_port(self) -> "Settings":
        """If `mcp_public_base_url` is still exactly its own static
        localhost default, rewrite its port to match `mcp_port` -- so
        setting only `MCP_PORT` (the common case: 8001 was already taken by
        something else) doesn't leave the two settings pointing at different
        ports for pure-local, no-ngrok use. A real deployment always sets
        `MCP_PUBLIC_BASE_URL` explicitly to its actual ngrok/public hostname
        (which has no port of its own to keep in sync with anything), so
        this never touches that case.
        """
        if self.mcp_public_base_url == self._DEFAULT_LOCAL_PUBLIC_BASE_URL and self.mcp_port != 8001:
            self.mcp_public_base_url = f"http://localhost:{self.mcp_port}"
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    Using a cached function rather than a module-level singleton keeps this
    override-able in tests (pytest fixtures can call
    `get_settings.cache_clear()` and monkeypatch environment variables
    per-test) without every other module needing to know that trick exists.
    """
    return Settings()
