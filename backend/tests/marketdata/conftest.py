from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.models import Candle, CandleInterval

BASE_TIME = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

MakeM1 = Callable[..., Candle]


@pytest.fixture
def make_m1() -> MakeM1:
    def factory(
        minutes_after_base: int,
        *,
        symbol: str = "BTC/USDT",
        open_quote: str = "100",
        high_quote: str = "110",
        low_quote: str = "90",
        close_quote: str = "105",
        volume_base: str = "1",
    ) -> Candle:
        open_time = BASE_TIME + timedelta(minutes=minutes_after_base)
        return Candle(
            symbol=symbol,
            interval=CandleInterval.M1,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=1),
            open_quote=Decimal(open_quote),
            high_quote=Decimal(high_quote),
            low_quote=Decimal(low_quote),
            close_quote=Decimal(close_quote),
            volume_base=Decimal(volume_base),
        )

    return factory
