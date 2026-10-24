"""Async SQLAlchemy engine, session management, and declarative base.

Owned by: database/ (ARCHITECTURE.md section 3 -- infrastructure layer, no
business logic, sits below every other module and can call nothing).

Every module that needs a DB session gets one through `get_db_session`
(the FastAPI dependency) or `session_scope` (for non-request contexts, e.g.
arq worker jobs) -- nothing outside this file constructs a session directly.
This is what keeps session lifecycle (commit/rollback/close) consistent
regardless of which module is doing the querying.

Cross-module discipline reminder (ARCHITECTURE.md section 2): a session
created here must never be passed across a module's public interface boundary
-- e.g. `core.get_incident(...)` returns a Pydantic model, not an ORM object
still attached to a session. That rule is enforced by convention in the
modules that use this file, not by anything in this file itself.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Query

from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings

logger = get_logger(__name__)


_TENANT_GUC_NAME = "app.current_organization_id"

_UNSUPPORTED_ASYNCPG_QUERY_PARAMS = {"sslmode", "channel_binding", "ssl"}

_SSL_DISABLED_VALUES = {"disable", "disabled", "false", "0", "off", "no"}


def _normalize_database_url(raw_url: str) -> str:
    """Strip libpq-only query parameters from a connection string before
    handing it to asyncpg.

    A permanent fix rather than asking every developer to hand-edit their
    `DATABASE_URL` to remove them: a connection string copy-pasted directly
    from Neon's dashboard should just work, unmodified.
    """
    parsed = urlsplit(raw_url)
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _UNSUPPORTED_ASYNCPG_QUERY_PARAMS
    ]
    return urlunsplit(parsed._replace(query=urlencode(kept_params)))


def _ssl_connect_arg(raw_url: str) -> bool:
    """Decide the `ssl` keyword `asyncpg.connect()` receives, from the URL's
    `sslmode` (preferred) or `ssl` query parameter.

    Returns `True` (TLS on) unless the parameter is explicitly one of
    `_SSL_DISABLED_VALUES` -- so an unset parameter stays secure, a managed
    provider's `?sslmode=require` keeps working unchanged, and only a
    deliberate `?sslmode=disable` (Railway private networking) turns TLS off.
    """
    params = {
        key.lower(): value
        for key, value in parse_qsl(urlsplit(raw_url).query, keep_blank_values=True)
    }
    requested = params.get("sslmode") or params.get("ssl") or ""
    return requested.strip().lower() not in _SSL_DISABLED_VALUES


class Base(DeclarativeBase):
    """Declarative base for every ORM model in app/database/models/.

    A single shared Base (rather than one per owning module) is deliberate:
    it's what lets Alembic's autogenerate see the entire schema in one
    metadata object, even though DATABASE_DESIGN.md's ownership rules keep
    each table's *write access* scoped to one module by convention.
    """


_COMMAND_TIMEOUT_SECONDS = 30.0
# Recycle pooled connections after 30 minutes -- `pool_pre_ping=True`
# already tests a connection is alive before handing it out, but does not
# bound how *old* a still-alive connection is; a managed Postgres provider
# (Neon) can terminate idle server-side connections on its own schedule
# independent of what the client believes is still open. Recycling
# proactively avoids relying solely on pre-ping's reactive detection.
_POOL_RECYCLE_SECONDS = 1800


def _build_engine(database_url: str | None = None) -> AsyncEngine:
    """Create an async engine from settings, or from `database_url` when
    given explicitly.

    Optional database URL override for this call, used by migrations to connect
    with the privileged migration role instead of the least-privileged runtime
    role. Ensures online migrations use `Settings.migration_database_url` rather
    than reusing the shared runtime engine."""

    settings = get_settings()
    url = database_url if database_url is not None else str(settings.database_url)
    return create_async_engine(
        _normalize_database_url(url),
        # SQL echo is intentionally independent from ENVIRONMENT. A full
        # ingestion emits several statements per document; synchronously
        # printing all of them can become a measurable local bottleneck.
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
        # asyncpg wants TLS as a connect-time keyword, not a URL query
        # parameter (see `_normalize_database_url` above). The value is
        # derived from the URL's own `sslmode`/`ssl` parameter by
        # `_ssl_connect_arg`: `True` for Neon and every other managed
        # provider (the default when unspecified), `False` only for a
        # deliberate `?sslmode=disable` such as Railway private networking,
        # where a non-SSL Postgres would fail outright with `ssl=True` forced.
        connect_args={
            "ssl": _ssl_connect_arg(url),
            "command_timeout": _COMMAND_TIMEOUT_SECONDS,
        },
    )


engine: AsyncEngine = _build_engine()
# Logged unconditionally at import time (not just on error) since this
# project runs against multiple, easily-confused DATABASE_URLs depending on
# how the process was started (root `.env` -> Neon vs `.env.docker` -> local
# Postgres container, see `.env.docker.example`'s own warning) -- printing
# host/dbname (never credentials) here makes "which database did this
# process actually connect to" a one-line grep instead of a debugging session.
logger.info(
    "database_engine_created",
    host=engine.url.host,
    database=engine.url.database,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, commits on success, rolls back
    and re-raises on exception, always closes.

    Usage in a module's repository.py:
        async def get_incident(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            # `BaseException`, not `Exception`: `asyncio.CancelledError` (e.g.
            # an arq `job_timeout` firing mid-job) is a `BaseException` in
            # Python >=3.8 and was previously falling through this handler
            # uncaught, skipping the rollback and leaving the transaction
            # open on the server until the connection was eventually
            # recycled -- exactly the kind of idle-in-transaction state that
            # can block a later job's writes to the same rows.
            await session.rollback()
            raise
        finally:
            await session.close()


async def set_tenant_context(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Sets the current organization ID for RLS policies for the current
       transaction only. Uses a bound parameter for safety and clears the
       tenant context on commit or rollback to prevent it leaking across
       requests that reuse pooled database connections.

       Must be called before querying any RLS-protected table. If omitted,
       RLS blocks access to protected rows by default.
    """
    await session.execute(
        text("SELECT set_config(:guc_name, :org_id, true)"),
        {"guc_name": _TENANT_GUC_NAME, "org_id": str(organization_id)},
    )


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context-manager equivalent of `get_db_session` for non-request
    contexts -- arq job handlers, the
    Knowledge Gap Agent's scheduled graph, CLI scripts, etc. -- anywhere
    FastAPI's `Depends` isn't available.

    Usage:
        async with session_scope() as session:
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:  # see get_db_session's matching handler above
            await session.rollback()
            raise
        finally:
            await session.close()
