"""Centralised logging configuration for the sidecar."""

import contextvars
import logging
import os
import time
import uuid
from contextlib import contextmanager

_configured = False

_ROOT_LOGGER_NAME = "mcp-sidecar"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    """Inject the current request ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()  # type: ignore[attr-defined]
        return True


def setup_logging() -> None:
    """Configure the root logger. Reads LOG_LEVEL env var (default INFO)."""
    global _configured
    if _configured:
        return
    _configured = True

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    root.setLevel(level)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [req:%(request_id)s] %(name)s %(filename)s:%(lineno)d: %(message)s"
    )
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the mcp-sidecar hierarchy."""
    setup_logging()
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


@contextmanager
def log_duration(logger: logging.Logger, operation: str):
    """Context manager that logs elapsed wall-clock time for *operation*."""
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("%s completed in %.0f ms", operation, elapsed_ms)


from fastmcp.server.middleware import Middleware as _FastMCPMiddleware
from fastmcp.server.middleware import MiddlewareContext as _MCtx
from fastmcp.server.middleware import CallNext as _CallNext


class McpRequestIdMiddleware(_FastMCPMiddleware):
    """Assign a unique request ID to every MCP operation."""

    async def on_message(self, context: _MCtx, call_next: _CallNext) -> object:
        token = request_id_var.set(str(uuid.uuid4()))
        try:
            return await call_next(context)
        finally:
            request_id_var.reset(token)
