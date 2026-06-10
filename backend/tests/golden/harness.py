"""Shared harness for the golden backtest: dataset, run, canonical encoding.

The golden test and the regeneration script must use exactly this code so
"regenerate" and "verify" can never drift apart.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tradebot.backtest import BacktestRunner, build_report
from tradebot.core.models import Candle, CandleInterval
from tradebot.execution import FillSimulatorConfig, SimulatedExecutionAdapter
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskConfig, RiskManager
from tradebot.strategies import TrendFollowingConfig, TrendFollowingStrategy

GOLDEN_SEED = 42
GOLDEN_CANDLE_COUNT = 1500
GOLDEN_INITIAL_BALANCE = Decimal("10000")
GOLDEN_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def make_golden_candles() -> list[Candle]:
    """A fixed, seeded random walk with drift segments so EMAs actually cross."""
    rng = random.Random(GOLDEN_SEED)
    candles: list[Candle] = []
    price = 100.0
    previous_close = price
    for index in range(GOLDEN_CANDLE_COUNT):
        # Alternating drift regimes make trends long enough to trade.
        drift = 0.0008 if (index // 250) % 2 == 0 else -0.0006
        price = max(1.0, price * (1.0 + drift + rng.gauss(0.0, 0.004)))
        open_price = Decimal(str(round(previous_close, 8)))
        close_price = Decimal(str(round(price, 8)))
        wick = Decimal(str(round(abs(rng.gauss(0.0, 0.002)) * price, 8)))
        open_time = GOLDEN_BASE_TIME + timedelta(minutes=index)
        candles.append(
            Candle(
                symbol="BTC/USDT",
                interval=CandleInterval.M1,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open_quote=open_price,
                high_quote=max(open_price, close_price) + wick,
                low_quote=max(min(open_price, close_price) - wick, Decimal("0.01")),
                close_quote=close_price,
                volume_base=Decimal(str(round(rng.uniform(1.0, 50.0), 8))),
            )
        )
        previous_close = price
    return candles


async def run_golden_backtest() -> str:
    """Run the fixed config over the fixed dataset; return canonical JSON."""
    candles = make_golden_candles()
    portfolio = Portfolio(GOLDEN_INITIAL_BALANCE)
    runner = BacktestRunner(
        TrendFollowingStrategy(TrendFollowingConfig()),
        RiskManager(RiskConfig(), portfolio),
        portfolio,
        SimulatedExecutionAdapter(FillSimulatorConfig()),
    )
    result = await runner.run(candles)
    report = build_report(result, candles, GOLDEN_INITIAL_BALANCE)

    payload: dict[str, Any] = {
        "fills": [
            {
                "client_order_id": fill.client_order_id,
                "side": fill.side.value,
                "price_quote": str(fill.price_quote),
                "quantity_base": str(fill.quantity_base),
                "fee_quote": str(fill.fee_quote),
                "filled_at": fill.filled_at.isoformat(),
            }
            for fill in result.fills
        ],
        "final_equity_quote": str(result.final_equity_quote),
        "realized_pnl_quote": str(result.realized_pnl_quote),
        "report": {
            "total_return_fraction": str(report.total_return_fraction),
            "max_drawdown_fraction": str(report.max_drawdown_fraction),
            "total_fees_quote": str(report.total_fees_quote),
            "round_trips": report.round_trips,
            "winning_round_trips": report.winning_round_trips,
            "buy_and_hold_return_fraction": str(report.buy_and_hold_return_fraction),
        },
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
