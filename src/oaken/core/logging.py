# SPDX-License-Identifier: Apache-2.0
"""Structured logging to stderr. Secrets are redacted and long values truncated, so mail
content cannot leak into logs by accident."""

import re
import sys
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from oaken.core.settings import LogSettings

REDACTED = "[REDACTED]"
MAX_FIELD = 200
_SENSITIVE = re.compile(r"token|secret|password|authorization", re.IGNORECASE)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def _clean(key: str, value: Any) -> Any:
    if _SENSITIVE.search(key):
        return REDACTED
    if isinstance(value, str):
        if len(value) > MAX_FIELD:
            return f"{value[:MAX_FIELD]}...[truncated {len(value) - MAX_FIELD} chars]"
        return value
    if isinstance(value, dict):
        return {k: _clean(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(key, v) for v in value]
    return value


def scrub(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Redact sensitive keys and truncate long values, at any depth."""
    return {k: _clean(k, v) for k, v in event_dict.items()}


def configure_logging(settings: LogSettings, stream: TextIO | None = None) -> None:
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            scrub,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[settings.level]),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
