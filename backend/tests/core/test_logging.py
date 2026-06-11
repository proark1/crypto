"""Structured logging tests: JSON shape, correlation fields, lossless money."""

import json
import logging
import sys
from decimal import Decimal

import pytest

from tradebot.core.logging import JsonLogFormatter, configure_logging, log_event
from tradebot.core.models import Side


def _record(**kwargs: object) -> logging.LogRecord:
    record = logging.makeLogRecord(
        {"name": "tradebot.engine", "levelno": logging.INFO, "levelname": "INFO", "msg": "hello"}
    )
    record.__dict__.update(kwargs)
    return record


class TestJsonLogFormatter:
    def test_base_fields_are_always_present(self) -> None:
        payload = json.loads(JsonLogFormatter().format(_record()))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "tradebot.engine"
        assert payload["message"] == "hello"
        assert payload["timestamp"].endswith("+00:00")  # UTC ISO-8601

    def test_extra_fields_are_promoted_to_top_level_keys(self) -> None:
        payload = json.loads(
            JsonLogFormatter().format(
                _record(event="order_submitted", signal_id="sig-1", client_order_id="ord-1")
            )
        )
        assert payload["event"] == "order_submitted"
        assert payload["signal_id"] == "sig-1"
        assert payload["client_order_id"] == "ord-1"

    def test_decimal_amounts_serialize_losslessly_as_strings(self) -> None:
        payload = json.loads(
            JsonLogFormatter().format(_record(quantity_base=Decimal("0.12345678")))
        )
        # A string, exact — never coerced to a lossy float.
        assert payload["quantity_base"] == "0.12345678"

    def test_str_enum_side_serializes_to_its_value(self) -> None:
        payload = json.loads(JsonLogFormatter().format(_record(side=Side.SELL)))
        assert payload["side"] == "sell"

    def test_exception_info_is_captured(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record()
            record.exc_info = sys.exc_info()
            payload = json.loads(JsonLogFormatter().format(record))
        assert "ValueError: boom" in payload["exc_info"]

    def test_every_line_is_a_single_json_object(self) -> None:
        line = JsonLogFormatter().format(_record(reasons="a; b"))
        assert "\n" not in line
        json.loads(line)  # parses


class TestLogEvent:
    def test_emits_event_as_message_with_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("tradebot.test.event")
        with caplog.at_level(logging.INFO, logger="tradebot.test.event"):
            log_event(logger, logging.INFO, "fill_recorded", client_order_id="ord-9", symbol="BTC")
        (record,) = caplog.records
        assert record.getMessage() == "fill_recorded"
        assert record.event == "fill_recorded"  # type: ignore[attr-defined]
        assert record.client_order_id == "ord-9"  # type: ignore[attr-defined]
        assert record.symbol == "BTC"  # type: ignore[attr-defined]

    def test_none_fields_are_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("tradebot.test.none")
        with caplog.at_level(logging.INFO, logger="tradebot.test.none"):
            log_event(logger, logging.INFO, "order_submitted", signal_id="s", client_order_id=None)
        (record,) = caplog.records
        assert record.signal_id == "s"  # type: ignore[attr-defined]
        assert not hasattr(record, "client_order_id")

    def test_exc_info_attaches_the_active_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("tradebot.test.exc")
        with caplog.at_level(logging.WARNING, logger="tradebot.test.exc"):
            try:
                raise ValueError("boom")
            except ValueError:
                log_event(logger, logging.WARNING, "backfill_failed", exc_info=True)
        (record,) = caplog.records
        assert record.exc_info is not None
        assert "ValueError: boom" in JsonLogFormatter().format(record)


class TestConfigureLogging:
    # configure_logging replaces the root handlers; save and restore pytest's
    # own (caplog/capture) handlers and level so later tests keep capturing.
    def setup_method(self) -> None:
        root = logging.getLogger()
        self._saved_handlers = root.handlers[:]
        self._saved_level = root.level

    def teardown_method(self) -> None:
        root = logging.getLogger()
        root.handlers[:] = self._saved_handlers
        root.setLevel(self._saved_level)

    def test_json_format_installs_the_json_formatter(self) -> None:
        configure_logging("DEBUG", "json")
        root = logging.getLogger()
        (handler,) = root.handlers
        assert isinstance(handler.formatter, JsonLogFormatter)
        assert root.level == logging.DEBUG

    def test_text_format_installs_a_plain_formatter(self) -> None:
        configure_logging("INFO", "text")
        (handler,) = logging.getLogger().handlers
        assert not isinstance(handler.formatter, JsonLogFormatter)

    def test_reconfigure_does_not_stack_handlers(self) -> None:
        configure_logging("INFO", "json")
        configure_logging("INFO", "json")
        assert len(logging.getLogger().handlers) == 1
