"""The strategy scenarios evaluate: the same shape production trades.

The worker routes between strategy families by market regime
(ARCHITECTURE.md §5.2); evaluating only the bare trend family would grade
a strategy the bot does not actually run. Scenarios cannot consult the
live reference-market detector — its state is wall-clock-bound and would
leak the present into a historical decision — so the router here
classifies the regime from the evaluated symbol's own candle stream,
using the production classifier and thresholds. The classification is
deterministic and leak-free: every input candle has already been shown to
the strategy.

Known divergence, on purpose: production routes every coin by the
*reference* market's regime (BTC by convention); a scenario routes by the
evaluated symbol's own. For the reference symbol the two are identical,
and for other coins a self-classified regime is the honest stand-in — the
reference market's candles at the scenario's moment are not part of the
scenario's blind window.
"""

from __future__ import annotations

from tradebot.core.models import Candle, Signal
from tradebot.portfolio import Position
from tradebot.signals import RegimeClassifier, RegimeConfig
from tradebot.strategies import (
    MeanReversionConfig,
    MeanReversionStrategy,
    RegimeStrategyRouter,
    Strategy,
    TrendFollowingConfig,
    TrendFollowingStrategy,
)


class SelfRoutedRegimeStrategy:
    """The production family router, fed by its own candle stream's regime.

    The classifier is created on the first candle (it labels its evidence
    with the symbol it watches, which a strategy factory cannot know);
    until ADX forms the regime reads ``warming_up`` and the router prefers
    the trend family, exactly as production does outside a ranging market.
    """

    def __init__(
        self,
        trend: Strategy,
        reversion: Strategy,
        regime_config: RegimeConfig | None = None,
    ) -> None:
        """Route ``trend``/``reversion`` by the self-classified regime."""
        self._regime_config = regime_config
        self._classifier: RegimeClassifier | None = None
        self._router = RegimeStrategyRouter(trend, reversion, regime_label=self._regime_label)

    @property
    def name(self) -> str:
        """The router's identifier; routed signals keep their family names."""
        return self._router.name

    def _regime_label(self) -> str:
        return self._classifier.regime.label if self._classifier is not None else "warming_up"

    def on_candle(self, candle: Candle, position: Position | None) -> Signal | None:
        """Classify on the candle just seen, then route exactly as production.

        Classification first: the live router also reads the regime as of
        the latest closed reference candle when it routes.
        """
        if self._classifier is None:
            self._classifier = RegimeClassifier(candle.symbol, self._regime_config)
        self._classifier.classify(candle)
        return self._router.on_candle(candle, position)


def build_traded_strategy(regime_routed: bool) -> Strategy:
    """Build one fresh instance of the strategy production would trade.

    ``regime_routed`` mirrors the worker's wiring: with the regime gate on,
    coins trade the family router; without it, the trend family alone. A
    fresh instance per call keeps indicator state from bleeding across
    scenarios (the :class:`~tradebot.evaluation.engine.ScenarioEvaluator`
    contract).
    """
    trend = TrendFollowingStrategy(TrendFollowingConfig())
    if not regime_routed:
        return trend
    return SelfRoutedRegimeStrategy(
        trend,
        MeanReversionStrategy(MeanReversionConfig()),
    )
