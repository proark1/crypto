from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.core.models import Candle, CandleInterval, Fill, Order, OrderType, Side, Signal

UTC_TIME = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def make_candle(**overrides: object) -> Candle:
    fields: dict[str, object] = {
        "symbol": "BTC/USDT",
        "interval": CandleInterval.M1,
        "open_time": UTC_TIME,
        "close_time": UTC_TIME + timedelta(minutes=1),
        "open_quote": Decimal("100"),
        "high_quote": Decimal("110"),
        "low_quote": Decimal("90"),
        "close_quote": Decimal("105"),
        "volume_base": Decimal("2.5"),
    }
    fields.update(overrides)
    return Candle.model_validate(fields)


class TestMoneyIsDecimal:
    def test_decimal_input_is_preserved_exactly(self) -> None:
        candle = make_candle(open_quote=Decimal("123.456789012345678901"))
        assert candle.open_quote == Decimal("123.456789012345678901")

    def test_string_input_is_accepted(self) -> None:
        candle = make_candle(open_quote="123.45")
        assert candle.open_quote == Decimal("123.45")

    def test_float_input_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="float is not allowed"):
            make_candle(open_quote=123.45)

    def test_float_rejected_for_signed_amounts_too(self) -> None:
        with pytest.raises(ValidationError, match="float is not allowed"):
            make_candle(volume_base=2.5)


class TestTimestampsAreUtc:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="naive datetime"):
            make_candle(open_time=datetime(2026, 1, 2, 3, 4))

    def test_non_utc_timezone_is_normalized_to_utc(self) -> None:
        plus_two = timezone(timedelta(hours=2))
        candle = make_candle(open_time=datetime(2026, 1, 2, 5, 4, tzinfo=plus_two))
        assert candle.open_time.tzinfo == UTC
        assert candle.open_time == UTC_TIME


class TestSignal:
    def test_stop_price_is_mandatory(self) -> None:
        with pytest.raises(ValidationError):
            Signal.model_validate(
                {
                    "strategy_name": "trend_following",
                    "symbol": "BTC/USDT",
                    "side": Side.BUY,
                    "confidence": 0.7,
                }
            )

    def test_zero_stop_price_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Signal(
                strategy_name="trend_following",
                symbol="BTC/USDT",
                side=Side.BUY,
                confidence=0.7,
                stop_price_quote=Decimal("0"),
            )

    def test_signal_ids_are_unique_and_created_at_is_utc(self) -> None:
        common = {
            "strategy_name": "trend_following",
            "symbol": "BTC/USDT",
            "side": Side.BUY,
            "confidence": 0.7,
            "stop_price_quote": Decimal("95"),
        }
        first = Signal.model_validate(common)
        second = Signal.model_validate(common)
        assert first.signal_id != second.signal_id
        assert first.created_at.tzinfo == UTC

    def test_confidence_outside_unit_interval_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Signal(
                strategy_name="trend_following",
                symbol="BTC/USDT",
                side=Side.BUY,
                confidence=1.5,
                stop_price_quote=Decimal("95"),
            )


class TestOrderAndFill:
    def test_order_requires_signal_lineage(self) -> None:
        with pytest.raises(ValidationError):
            Order.model_validate(
                {
                    "client_order_id": "order-1",
                    "symbol": "BTC/USDT",
                    "side": Side.BUY,
                    "order_type": OrderType.LIMIT,
                    "quantity_base": Decimal("0.01"),
                }
            )

    def test_order_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Order(
                client_order_id="order-1",
                signal_id="sig-1",
                symbol="BTC/USDT",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity_base=Decimal("0"),
            )

    def test_models_are_immutable(self) -> None:
        fill = Fill(
            client_order_id="order-1",
            symbol="BTC/USDT",
            side=Side.BUY,
            price_quote=Decimal("100"),
            quantity_base=Decimal("0.01"),
            fee_quote=Decimal("0.1"),
            filled_at=UTC_TIME,
        )
        with pytest.raises(ValidationError):
            fill.price_quote = Decimal("200")  # type: ignore[misc]
