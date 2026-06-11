"""Structured logging: one JSON event per line, with correlation fields.

The bot moves real money, so when something goes wrong the logs have to be
queryable — "show every line for this signal id" across the signal → order →
fill chain, not a grep through prose. This module provides a stdlib-only JSON
formatter and a tiny helper that attaches structured fields to a record; no
third-party logging dependency (CLAUDE.md keeps the dependency set minimal).

Field convention (use these names so a query means the same thing everywhere):
``event`` (a stable snake_case key), ``symbol``, ``signal_id``,
``client_order_id``, ``bot_id``, ``strategy_name``, ``side``, ``quantity_base``.
Amounts stay ``Decimal`` and serialize losslessly as strings. Never put a
secret in a field — the same rule as the rest of the codebase.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

# Every attribute the stdlib puts on a LogRecord. Anything *else* on a record
# is a structured field someone attached via ``extra=`` and belongs in the
# JSON payload. Derived from a real record so it tracks the running Python
# version instead of a hand-maintained (and drift-prone) list.
_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
"""Human-readable fallback for local development (``TRADEBOT_LOG_FORMAT=text``)."""


class JsonLogFormatter(logging.Formatter):
    """Render each record as a single-line JSON object.

    The payload always carries ``timestamp`` (UTC ISO-8601), ``level``,
    ``logger``, and ``message``; any structured fields attached via ``extra=``
    follow. ``Decimal`` (money) and other non-JSON types serialize via ``str``
    so amounts are never silently coerced to float.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize ``record`` (plus its ``extra=`` fields) to a JSON line."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single root handler with the chosen formatter.

    Replaces any existing handlers so a re-configure (or a library's stray
    ``basicConfig``) cannot double every line. ``fmt`` is ``"json"`` (default,
    for production log aggregation) or ``"text"`` (readable local tailing).
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def log_event(
    logger: logging.Logger, level: int, event: str, /, *, exc_info: bool = False, **fields: Any
) -> None:
    """Emit a structured event: ``message`` is ``event``, fields ride alongside.

    ``None`` fields are dropped so an absent correlation id does not clutter
    the line. ``exc_info=True`` attaches the active exception (the formatter
    renders it under ``exc_info``). Field names must avoid stdlib ``LogRecord``
    attributes (``name``, ``module``, ``args``, ...); the documented convention
    names are all safe.
    """
    extra = {"event": event, **{key: value for key, value in fields.items() if value is not None}}
    logger.log(level, event, exc_info=exc_info, extra=extra)
