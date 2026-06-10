"""In-process metrics for the /metrics endpoint (ARCHITECTURE.md 4.9).

Hand-rolled Prometheus text format: the metric set is small and a client
library would be the only new dependency in the hot path's vicinity.
Counters are fed by bus subscriptions (observers only — they can never
slow or break trading); gauges are computed at scrape time from the same
state the dashboard reads, so the two can never disagree.
"""

from __future__ import annotations

from collections import Counter

from tradebot.core.events import CandleClosed, EventBus, FillRecorded, ProposalCreated


def format_metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    """Render one sample in Prometheus text exposition format."""
    if labels:
        rendered = ",".join(f'{key}="{value_}"' for key, value_ in sorted(labels.items()))
        return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


class MetricsCollector:
    """Counts bus traffic; attach once per worker, scrape via the API."""

    def __init__(self) -> None:
        """Create zeroed counters."""
        self.candles_total: Counter[str] = Counter()
        self.fills_total: Counter[tuple[str, str]] = Counter()
        self.proposals_total = 0

    def attach_to(self, bus: EventBus) -> None:
        """Subscribe the counters to the worker's bus."""
        bus.subscribe(CandleClosed, self._on_candle)
        bus.subscribe(FillRecorded, self._on_fill)
        bus.subscribe(ProposalCreated, self._on_proposal)

    async def _on_candle(self, event: CandleClosed) -> None:
        self.candles_total[event.candle.symbol] += 1

    async def _on_fill(self, event: FillRecorded) -> None:
        self.fills_total[(event.fill.symbol, event.fill.side.value)] += 1

    async def _on_proposal(self, event: ProposalCreated) -> None:
        self.proposals_total += 1

    def render_counters(self) -> list[str]:
        """Render the counter samples (gauges are the API's job)."""
        lines = [
            "# TYPE tradebot_candles_processed_total counter",
            *(
                format_metric("tradebot_candles_processed_total", count, {"symbol": symbol})
                for symbol, count in sorted(self.candles_total.items())
            ),
            "# TYPE tradebot_fills_total counter",
            *(
                format_metric("tradebot_fills_total", count, {"side": side, "symbol": symbol})
                for (symbol, side), count in sorted(self.fills_total.items())
            ),
            "# TYPE tradebot_proposals_created_total counter",
            format_metric("tradebot_proposals_created_total", self.proposals_total),
        ]
        return lines
