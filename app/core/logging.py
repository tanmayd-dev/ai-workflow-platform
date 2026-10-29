"""Structured logging with per-request correlation ids.

Dependency-free: stdlib ``logging`` + ``json``. The request id travels in a
``contextvars.ContextVar``; a ``logging.Filter`` copies it onto every log
record (``%(request_id)s``), so Starlette middleware can scope it per request
and it still propagates into background tasks and ``await``-ed coroutines.

Env knobs (see ``app.core.config.Settings``):
- ``LOG_LEVEL``: "DEBUG"/"INFO"/"WARNING"/"ERROR" (default INFO)
- ``LOG_JSON``: when true, emit JSON lines (keys: ts/level/logger/request_id/message)
"""
import json
import logging
import sys
from contextvars import ContextVar

_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Rendered instead of an absent id, so log lines never split mid-token.
_NO_REQUEST_ID = "-"

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [req:%(request_id)s] %(message)s"

_configured = False


def set_request_id(request_id: str) -> "object":
    """Set the request id for the current async context; token for reset."""
    return _request_id_ctx.set(request_id)


def reset_request_id(token) -> None:
    """Reset the request id contextvar to its previous value."""
    _request_id_ctx.reset(token)


def get_request_id() -> str | None:
    """Return the request id of the current async context (or None)."""
    return _request_id_ctx.get()


class RequestIdFilter(logging.Filter):
    """Copy the request id from the contextvar onto ``record.request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get() or _NO_REQUEST_ID
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record (JSON Lines-stream friendly)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", _NO_REQUEST_ID),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _resolve_level(raw) -> int:
    """Map a LOG_LEVEL string to a logging level, falling back to INFO."""
    level = logging.getLevelName(str(raw).upper())
    if isinstance(level, int):
        return level
    return logging.INFO


def configure_logging() -> None:
    """Configure the root logger once. Safe to call repeatedly (guarded).

    Adds a single stdout handler (format chosen by LOG_JSON) whose filter
    injects the request id; uvicorn's loggers keep propagating through root,
    so those lines pick up the same format and request id as app logs.
    """
    global _configured
    if _configured:
        return

    from app.core.config import get_settings

    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.LOG_JSON else logging.Formatter(DEFAULT_LOG_FORMAT)
    )
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.setLevel(_resolve_level(settings.LOG_LEVEL))
    root.addHandler(handler)
    _configured = True
