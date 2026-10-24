"""Structured logging configuration.

Owned by: shared/ (ARCHITECTURE.md section 3 -- cross-cutting, no business
meaning of its own, importable by every other module).

This is the second shared foundation piece after settings.py. Every module
that logs anything (core, agents, ingestion, mcp, retrieval) should call
`get_logger(__name__)` rather than importing `logging` or `structlog`
directly -- that's what keeps the log format/behavior consistent regardless
of which module is doing the logging, without every module needing to know
the configuration details below.

Two output modes, chosen by `Settings.environment`:
  - development / test -> human-readable, colored console output. Optimized
    for a solo developer reading logs in a terminal while iterating.
  - production          -> structured JSON, one event per line. Optimized for
    ingestion by a log aggregator, not for human eyes.

`Settings.log_level` (DATABASE/REDIS/etc. failures, agent errors, etc.) sets
the minimum level emitted, consistent with the settings module already
loaded before this one -- logging depends on settings, not the reverse.
"""

import logging
import re
import sys
from typing import Any

import structlog

from app.shared.config.settings import get_settings

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|token|secret|api_key|apikey|authorization|credential|client_secret|private_key)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


def _redact_sensitive_fields(
    logger: object, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor: redact any event-dict key whose name matches
    `_SENSITIVE_KEY_PATTERN`, at any nesting depth (dicts only -- lists of
    scalars are left alone, since no call site in this codebase logs
    secrets inside a list).
    """

    def _scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (_REDACTED if _SENSITIVE_KEY_PATTERN.search(key) else _scrub(inner))
                for key, inner in value.items()
            }
        return value

    return {
        key: (_REDACTED if _SENSITIVE_KEY_PATTERN.search(key) else _scrub(value))
        for key, value in event_dict.items()
    }


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Call once, at process startup.

    Safe to call multiple times (e.g. once from the API process, once from
    the arq worker entrypoint per ENGINEERING_DECISIONS.md #002) -- each call
    just re-applies the same configuration.
    """
    settings = get_settings()
    is_production = settings.environment == "production"

    # Shared processor chain: everything that adds context to a log event,
    # regardless of final rendering (JSON vs console).
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_sensitive_fields,
    ]

    if is_production:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            # Prepares the event dict for stdlib's formatter, which then
            # applies `renderer` below. This is what lets stdlib-originated
            # logs (uvicorn, sqlalchemy, arq) go through the same renderer
            # as structlog-originated ones, instead of looking different.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Strips structlog-internal metadata before final rendering, then
        # renders with whichever renderer was chosen above.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level)

    # Quiet down noisy third-party loggers that would otherwise clutter
    # output at INFO level (connection pool chatter, etc.) without hiding
    # anything we'd actually want to see during development.
    for noisy_logger in ("sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(noisy_logger).setLevel(
            logging.WARNING if not is_production else logging.ERROR
        )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to `name` (conventionally `__name__`).

    This is the only logging entrypoint other modules should use -- it
    guarantees every log event goes through the same configuration applied
    by `configure_logging()`, regardless of which module calls it...
    """
    return structlog.get_logger(name)