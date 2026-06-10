"""Market data ingestion and candle machinery.

The 1m candle is the base unit of truth: the exchange feed (or backtest
dataset) supplies closed 1m candles, ``TimeframeAggregator`` rolls them up to
the higher intervals strategies consume, ``find_gaps`` tells the backfill
which ranges to fetch over REST, and ``validate_candle`` flags data the bot
must quarantine rather than trade on (ARCHITECTURE.md section 11).
"""

from tradebot.marketdata.aggregation import TimeframeAggregator
from tradebot.marketdata.gaps import find_gaps
from tradebot.marketdata.validation import validate_candle

__all__ = ["TimeframeAggregator", "find_gaps", "validate_candle"]
