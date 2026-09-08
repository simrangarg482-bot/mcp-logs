"""FastAPI application factory for EKIP's REST API.

Owned by: app/api. Per API_DESIGN.md section 1 and ARCHITECTURE.md section 6:
REST and MCP are thin, parallel wrappers around the same core/agents
Pydantic-typed internal interfaces -- this module's routers contain no
business logic beyond request/response translation, matching MCP's tool
handlers' own "no logic beyond this translation" rule.

Scope of what's wired up here (see each router's own docstring for what's
excluded and why): auth (the real SSO/PKCE flow core/auth actually
implements, superseding API_DESIGN.md's older `/auth/login`
username+password sketch), incidents (full CRUD + timeline), ask
(`answer_question` + `triage_incident`), postmortems (generate/read/edit/
approve), knowledge (the review queue -- list/publish/reject/gaps),
observability (`/observability/agents`, `/observability/mcp` -- the
"dashboards, latency metrics" requirement, PROJECT_PLAN.md section 10,
Milestone 10), tenancy (`/tenancy/connectors` -- registering and listing
connector configurations, the previously-missing REST surface for
`core.tenancy.service.register_connector`/`list_connectors`, closed as a
follow-up after Milestone 10), tenancy's `admin_router` (organizations,
projects, SSO configuration, access rules, invitations -- the rest of
`core.tenancy.service`'s previously-unreachable surface, closed in the same
integration-gaps pass that added project-scoped RBAC and logout-everywhere),
and users (`/users/{user_id}/logout-all` -- the admin-triggered session
revocation counterpart to `/auth/logout-all`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import ekip_error_handler
from app.api.middleware import RequestContextMiddleware
from app.api.routers import (
    ask,
    auth,
    graph,
    health,
    incidents,
    insights,
    knowledge,
    observability,
    postmortems,
    tenancy,
    memory,
    users,
)
from app.core.exceptions import EKIPError
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.config.tracing import configure_tracing
from app.shared.redis_settings import build_redis_settings

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Owns the one `arq` Redis pool this process uses to *enqueue* jobs
    (`POST /tenancy/connectors/{id}/sync`, `app.api.deps.get_arq_pool`) --
    not to run them. Running jobs is `scripts/ingestion_worker.py`'s /
    `app.ingestion.workers.main.WorkerSettings`'s job, a separate process,
    exactly as `app.ingestion.workers.main`'s own docstring already
    establishes (ENGINEERING_DECISIONS.md #002: API server and worker are
    separate processes sharing one Redis queue, not one process doing both).
    Built from the same `Settings.redis_url` that worker already reads, so
    there is one source of truth for the connection string, not a second one
    hand-maintained here.

    `default_queue_name` must match `app.ingestion.workers.main.WorkerSettings
    .queue_name` ("arq:queue:ingestion"), the only worker that registers
    `run_ingestion_job_task` -- the sole function this pool ever enqueues.
    Without it, `create_pool` falls back to arq's own hardcoded default
    ("arq:queue"), which no worker polls (both workers opted out of that
    default for the queue-collision reason documented on their own
    `queue_name` attributes), so every job enqueued here would sit in Redis
    forever and connector syncs would silently never run.

    A failed connection here does NOT fail app startup: `create_pool`
    previously ran unguarded, so a real, observed Redis Cloud connection
    blip (this dependency's provider intermittently timing out, independent
    of anything this app does) took down 100% of the API -- login,
    incidents, knowledge, everything -- not just the ~1% of functionality
    (connector sync) that actually needs Redis. Caught by an actual browser
    E2E run (`frontend/e2e/`) where the whole app stopped responding during
    a live Redis outage; nothing in the unit test suite ever exercises real
    process startup against a real (or really-unreachable) Redis. On
    failure, `app.state.arq_pool` is left `None` and `get_arq_pool` raises a
    clean `ServiceUnavailableError` (503) only for the one request path that
    actually needs it.
    """
    try:
        app.state.arq_pool = await create_pool(
            build_redis_settings(),
            default_queue_name="arq:queue:ingestion",
        )
        # This pool only ever enqueues (`sync_connector`) -- unlike the
        # ingestion/agents worker processes, which are constantly polling
        # Redis, it can sit fully idle for many minutes between sync-now
        # clicks. Redis Cloud silently drops a connection idle that long,
        # and redis-py can then hand that dead connection back to the next
        # `enqueue_job()` call, which returns normally (a clean `202`) even
        # though nothing ever reached Redis -- observed in practice after
        # ~20-30 minutes of inactivity between clicks. `health_check_interval`
        # makes redis-py PING a connection that has been idle this long
        # before reusing it, replacing it if the PING fails, instead of
        # silently handing back a dead one. Not exposed via arq's own
        # `RedisSettings`/`create_pool` (see `build_redis_settings`'s own
        # docstring for why every Redis-connecting process already goes
        # through that shared builder instead of a bare `RedisSettings.
        # from_dsn`), so it's set directly on the pool's connection kwargs
        # here and applied immediately by disconnecting the pool's initial
        # (ping-only) connection -- every connection this pool ever hands
        # out afterward is created fresh from these kwargs, health check
        # included.
        app.state.arq_pool.connection_pool.connection_kwargs["health_check_interval"] = 30
        await app.state.arq_pool.connection_pool.disconnect()
    except Exception as exc:
        # Not `exc_info=True`: structlog's console renderer writes through
        # Python's default stdout encoding, which on Windows is the legacy
        # `cp1252` codepage -- a full traceback can (and, observed here,
        # did) contain a character `cp1252` can't encode, crashing the
        # logging call itself and turning a handled Redis failure back into
        # an unhandled one. `str(exc)` is plain ASCII-safe text.
        logger.warning("arq_pool_unavailable_at_startup", error=str(exc))
        app.state.arq_pool = None
    try:
        yield
    finally:
        if app.state.arq_pool is not None:
            await app.state.arq_pool.close()


def create_app() -> FastAPI:
    app = FastAPI(title="EKIP API", version="0.1.0", lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last (Starlette applies the most-recently-added middleware
    # outermost) so request correlation wraps everything, including CORS
    # handling -- the request id/duration this middleware logs should
    # reflect the true total request span, not just the part after CORS
    # already ran.
    app.add_middleware(RequestContextMiddleware)

    app.add_exception_handler(EKIPError, ekip_error_handler)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(incidents.router)
    app.include_router(ask.router)
    app.include_router(postmortems.router)
    app.include_router(knowledge.router)
    app.include_router(observability.router)
    app.include_router(tenancy.router)
    app.include_router(tenancy.admin_router)
    app.include_router(users.router)
    app.include_router(memory.router)
    app.include_router(graph.router)
    app.include_router(insights.router)

    configure_tracing(app)

    return app


app = create_app()
