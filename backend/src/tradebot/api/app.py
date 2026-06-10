"""FastAPI app factory for the control plane.

Amounts are serialized as strings (Decimal-safe — the frontend never does
money arithmetic on floats, CLAUDE.md frontend rules). The app depends on a
narrow ``BotState`` protocol rather than the worker class itself, so it can
be tested with any object exposing the same surface and never imports the
composition root.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from tradebot.core.config import AppConfig
from tradebot.core.metrics import MetricsCollector, format_metric
from tradebot.core.models import Candle, CandleInterval, utc_now
from tradebot.engine import TradingEngine
from tradebot.evaluation.models import LearningFinding
from tradebot.evaluation.replay import load_replay
from tradebot.evaluation.runner import EvaluationRunConfig
from tradebot.evaluation.sweep import DEFAULT_TREND_CANDIDATES, SweepCandidate, SweepConfig
from tradebot.news import NewsFlags
from tradebot.persistence import CandleStore, DecisionStore, EvaluationStore, FillStore
from tradebot.portfolio import Portfolio


class BotState(Protocol):
    """What the control plane is allowed to see of the running bot."""

    @property
    def config(self) -> AppConfig:
        """Runtime configuration (mode, symbol, exchange)."""
        ...

    @property
    def portfolio(self) -> Portfolio:
        """Live positions, balances, and PnL."""
        ...

    @property
    def candle_store(self) -> CandleStore:
        """Persisted candles; the newest close is the mark price."""
        ...

    @property
    def fill_store(self) -> FillStore:
        """The persistent fill journal."""
        ...

    @property
    def engines(self) -> Mapping[str, TradingEngine]:
        """One trading loop per symbol, for pause/resume/kill commands."""
        ...

    async def add_coin(self, symbol: str) -> None:
        """Start trading a coin at runtime (``ValueError`` on bad input)."""
        ...

    async def remove_coin(self, symbol: str) -> None:
        """Stop trading a coin (``KeyError`` unknown, ``RuntimeError`` unsafe)."""
        ...

    @property
    def decision_store(self) -> DecisionStore:
        """The explainability trail: every signal and its fate."""
        ...

    @property
    def evaluation_store(self) -> EvaluationStore:
        """Persisted evaluation runs, scenarios, and verdicts."""
        ...

    async def start_evaluation(self, config: EvaluationRunConfig) -> int:
        """Start a run (``RuntimeError`` if one is in flight, ``ValueError`` bad config)."""
        ...

    def cancel_evaluation(self, run_id: int) -> bool:
        """Cancel the in-flight run; False when it is not running."""
        ...

    async def start_sweep(self, config: SweepConfig) -> int:
        """Start a sweep (``RuntimeError`` if one is in flight, ``ValueError`` bad config)."""
        ...

    def cancel_sweep(self, sweep_id: int) -> bool:
        """Cancel the in-flight sweep; False when it is not running."""
        ...

    @property
    def metrics(self) -> MetricsCollector:
        """Bus-fed counters for the /metrics endpoint."""
        ...

    @property
    def news_flags(self) -> NewsFlags:
        """Active negative-news flags (gauge + status surfaces)."""
        ...


class PositionResponse(BaseModel):
    """One open position, amounts as strings."""

    symbol: str
    quantity_base: str
    average_entry_price_quote: str
    unrealized_pnl_quote: str | None


class BreakersResponse(BaseModel):
    """Circuit-breaker state: why entries are blocked, if they are."""

    tripped_reason: str | None
    cooldown_until: str | None
    entries_today: int


class StatusResponse(BaseModel):
    """The three-second answer to "is everything okay?"."""

    mode: str
    paused: bool
    symbol: str
    symbols: list[str]
    exchange_id: str
    quote_currency: str
    quote_balance: str
    realized_pnl_quote: str
    position: PositionResponse | None
    last_candle_close_time: str | None
    mark_price_quote: str | None
    equity_quote: str | None
    breakers: BreakersResponse


class CommandResponse(BaseModel):
    """Outcome of a control command."""

    paused: bool
    detail: str


class DecisionResponse(BaseModel):
    """One signal and its fate, with the reasons shown verbatim."""

    signal_id: str
    strategy_name: str
    symbol: str
    side: str
    stop_price_quote: str
    reasons: list[str]
    outcome: str
    created_at: str


class CoinActionRequest(BaseModel):
    """Names the coin to add or remove (in the body: symbols contain ``/``)."""

    symbol: str


class ProposalActionRequest(BaseModel):
    """Identifies the proposal to act on.

    In the body rather than the path: signal ids contain the symbol (e.g.
    ``trend_following:BTC/USDT:...``), whose slash would break path routing.
    """

    signal_id: str


class ProposalResponse(BaseModel):
    """One pending co-pilot proposal awaiting approve/reject."""

    signal_id: str
    symbol: str
    side: str
    strategy_name: str
    proposal_price_quote: str
    stop_price_quote: str
    reasons: list[str]
    created_at: str
    expires_at: str


class CandleResponse(BaseModel):
    """One OHLCV candle for charting, amounts as strings."""

    open_time: str
    open_quote: str
    high_quote: str
    low_quote: str
    close_quote: str
    volume_base: str


class EvaluationStartRequest(BaseModel):
    """Shape of a new evaluation run; symbols default to the active coins."""

    symbols: list[str] | None = None
    timeframes: list[str] = ["1h"]
    history_days: int = 365
    scenario_count: int = 200
    lookback_candles: int = 200
    horizon_candles: int = 60
    seed: int = 7


class EvaluationStartResponse(BaseModel):
    """Acknowledgement that a run was created and launched."""

    run_id: int
    detail: str


class EvaluationRunResponse(BaseModel):
    """One run's status, progress, and (when completed) its report."""

    id: int
    created_at: str
    status: str
    symbols: list[str]
    timeframes: list[str]
    progress_done: int
    progress_total: int
    config: dict[str, Any]
    summary: dict[str, Any] | None


def _run_response(run: dict[str, Any]) -> EvaluationRunResponse:
    """Serialize a run row for the API."""
    return EvaluationRunResponse(
        id=run["id"],
        created_at=run["created_at"].isoformat(),
        status=run["status"],
        symbols=list(run["symbols"]),
        timeframes=list(run["timeframes"]),
        progress_done=run["progress_done"],
        progress_total=run["progress_total"],
        config=run["config"],
        summary=run["summary"],
    )


class ScenarioSummaryResponse(BaseModel):
    """One graded scenario row for the replay browser, amounts as strings."""

    scenario_id: int
    run_id: int
    symbol: str
    timeframe: str
    decision_time: str
    scenario_class: str
    trend: str
    volatility: str
    events: list[str]
    decision: str
    verdict: str
    r_multiple: str | None
    timing: str | None


class ScenarioReplayResponse(BaseModel):
    """Everything the replay viewer needs for one scenario.

    ``window`` is the blind context the bot decided on (its last candle
    closes at the decision time); ``horizon`` is the future it was graded
    against, for the viewer to reveal candle by candle.
    """

    scenario: ScenarioSummaryResponse
    confidence: float | None
    reasons: list[str]
    entry_price_quote: str | None
    exit_price_quote: str | None
    pnl_quote: str | None
    mfe_r: str | None
    mae_r: str | None
    duration_candles: int | None
    stop_hit: bool | None
    oracle_r: str | None
    window: list[CandleResponse]
    horizon: list[CandleResponse]


def _optional_str(value: Any) -> str | None:
    """Stringify a nullable Decimal column without inventing a zero."""
    return None if value is None else str(value)


def _scenario_summary(row: Mapping[str, Any]) -> ScenarioSummaryResponse:
    """Serialize one joined scenario+result row for the API."""
    return ScenarioSummaryResponse(
        scenario_id=row["scenario_id"],
        run_id=row["run_id"],
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        decision_time=row["decision_time"].isoformat(),
        scenario_class=row["scenario_class"],
        trend=row["trend"],
        volatility=row["volatility"],
        events=list(row["events"]),
        decision=row["decision"],
        verdict=row["verdict"],
        r_multiple=_optional_str(row["r_multiple"]),
        timing=row["timing"],
    )


class SweepCandidateRequest(BaseModel):
    """One named parameter set to compete in a sweep."""

    name: str
    params: dict[str, Any]


class SweepStartRequest(BaseModel):
    """Shape of a new sweep; the symbol defaults to the first active coin.

    Omitting ``candidates`` sweeps the trend-following family's default
    grid; ``candidates[0]`` is always treated as the baseline.
    """

    symbol: str | None = None
    timeframe: str = "1h"
    history_days: int = 180
    scenario_count: int = 100
    lookback_candles: int = 200
    horizon_candles: int = 60
    seed: int = 7
    training_fraction: float = 0.7
    candidates: list[SweepCandidateRequest] | None = None
    motivating_finding_ids: list[int] = []


class SweepResponse(BaseModel):
    """One sweep's status and (when completed) its walk-forward report."""

    id: int
    created_at: str
    status: str
    symbol: str
    timeframe: str
    config: dict[str, Any]
    motivating_finding_ids: list[int]
    report: dict[str, Any] | None


def _sweep_response(sweep: Mapping[str, Any]) -> SweepResponse:
    """Serialize a sweep row for the API."""
    return SweepResponse(
        id=sweep["id"],
        created_at=sweep["created_at"].isoformat(),
        status=sweep["status"],
        symbol=sweep["symbol"],
        timeframe=sweep["timeframe"],
        config=sweep["config"],
        motivating_finding_ids=list(sweep["motivating_finding_ids"]),
        report=sweep["report"],
    )


class FindingResponse(BaseModel):
    """One mined mistake pattern awaiting (or carrying) the human verdict."""

    id: int
    run_id: int
    pattern: str
    evidence_scenario_ids: list[int]
    affected_count: int
    average_r_impact: str
    suggestion: str
    confidence: str
    status: str
    created_at: str


def _finding_response(finding_id: int, finding: LearningFinding) -> FindingResponse:
    """Serialize one finding for the API."""
    return FindingResponse(
        id=finding_id,
        run_id=finding.run_id,
        pattern=finding.pattern,
        evidence_scenario_ids=list(finding.evidence_scenario_ids),
        affected_count=finding.affected_count,
        average_r_impact=str(finding.average_r_impact),
        suggestion=finding.suggestion,
        confidence=finding.confidence,
        status=finding.status,
        created_at=finding.created_at.isoformat(),
    )


def _candle_response(candle: Candle) -> CandleResponse:
    """Serialize one candle for charting, amounts as strings."""
    return CandleResponse(
        open_time=candle.open_time.isoformat(),
        open_quote=str(candle.open_quote),
        high_quote=str(candle.high_quote),
        low_quote=str(candle.low_quote),
        close_quote=str(candle.close_quote),
        volume_base=str(candle.volume_base),
    )


class FillResponse(BaseModel):
    """One journaled fill, amounts as strings."""

    client_order_id: str
    symbol: str
    side: str
    price_quote: str
    quantity_base: str
    fee_quote: str
    filled_at: str


def _parse_cors_origins(raw: str) -> list[str]:
    """Split the comma-separated origins setting, ignoring blanks.

    Trailing slashes are stripped because an Origin header never carries
    one — ``https://app.example.com/`` pasted from a browser address bar
    would otherwise silently never match.
    """
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def create_health_only_app() -> FastAPI:
    """Build a liveness-only app for when the control plane is disabled.

    The platform healthcheck must work in every configuration; running a
    bot whose deploy can never be marked healthy just because the API token
    is unset would be a trap.
    """
    app = FastAPI(title="tradebot health")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def create_app(state: BotState, api_token: str) -> FastAPI:
    """Build the control-plane app; every route requires the bearer token."""
    if not api_token:
        raise ValueError("control API requires a non-empty token; refusing to build")

    bearer = HTTPBearer(auto_error=False)

    def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        # compare_digest keeps token comparison constant-time.
        if credentials is None or not secrets.compare_digest(credentials.credentials, api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
            )

    def active_symbols() -> list[str]:
        """Return the live coin set.

        Read fresh per request, never captured: coins are added and removed
        at runtime.
        """
        return list(state.engines)

    def resolve_symbol(symbol: str | None) -> str:
        """Default to the first active symbol; 404 unknown ones."""
        symbols = active_symbols()
        if not symbols:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no coins are active")
        if symbol is None:
            return symbols[0]
        if symbol not in state.engines:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown symbol {symbol!r}; active: {symbols}",
            )
        return symbol

    async def account_equity() -> Decimal | None:
        """Mark every open position at its latest stored close; ``None`` if any lacks one.

        Marks are gathered for every *active* symbol first, and only then
        is the portfolio read — synchronously, with no awaits in between. An
        await between reading positions and valuing them would let a fill on
        the trading loop open a position the marks don't cover, turning a
        status request into a 500.
        """
        marks: dict[str, Decimal] = {}
        for active in active_symbols():
            candle = await state.candle_store.latest_candle(active, CandleInterval.M1)
            if candle is not None:
                marks[active] = candle.close_quote
        for open_symbol in state.portfolio.positions:
            if open_symbol not in marks:
                return None  # refuse to guess, never wrong
        return state.portfolio.equity_quote(marks)

    app = FastAPI(title="tradebot control plane")
    # The dashboard is served from a different origin than the API (two
    # Railway services), so without these headers the browser blocks every
    # request before it leaves the page. Credentials stay off: auth is a
    # bearer header the page attaches itself, never a cookie the browser
    # would attach for an attacker. With credentials off, wildcard methods
    # and headers are safe — and they keep preflights working when the
    # frontend grows headers (tracing, monitoring) or the API grows verbs;
    # the real gate is the bearer token, not the preflight.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors_origins(state.config.api_cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    protected = APIRouter(dependencies=[Depends(require_token)])

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Public liveness probe: platform healthchecks cannot send tokens.

        Minimal by design: even mode and symbol are operational details an
        unauthenticated scanner has no business learning. Everything real
        stays behind the bearer token.
        """
        return {"status": "ok"}

    @protected.get("/status")
    async def get_status(symbol: str | None = Query(None)) -> StatusResponse:
        portfolio = state.portfolio
        selected = resolve_symbol(symbol)
        engine = state.engines[selected]
        latest = await state.candle_store.latest_candle(selected, CandleInterval.M1)
        position = portfolio.position(selected)

        mark_price = latest.close_quote if latest is not None else None
        # Account-wide equity: every open position (any symbol) marked at
        # its latest stored close.
        equity = await account_equity()
        position_response = None
        if position is not None:
            position_response = PositionResponse(
                symbol=position.symbol,
                quantity_base=str(position.quantity_base),
                average_entry_price_quote=str(position.average_entry_price_quote),
                unrealized_pnl_quote=(
                    str(position.unrealized_pnl_quote(mark_price))
                    if mark_price is not None
                    else None
                ),
            )
        breakers = engine.breakers  # one shared account-level instance
        return StatusResponse(
            mode=state.config.mode.value,
            paused=engine.paused,
            breakers=BreakersResponse(
                tripped_reason=breakers.tripped_reason,
                cooldown_until=(
                    breakers.cooldown_until.isoformat()
                    if breakers.cooldown_until is not None
                    else None
                ),
                entries_today=breakers.entries_today,
            ),
            symbol=selected,
            symbols=active_symbols(),
            exchange_id=state.config.exchange_id,
            quote_currency=state.config.quote_currency,
            quote_balance=str(portfolio.quote_balance),
            realized_pnl_quote=str(portfolio.realized_pnl_quote()),
            position=position_response,
            last_candle_close_time=(latest.close_time.isoformat() if latest is not None else None),
            mark_price_quote=str(mark_price) if mark_price is not None else None,
            equity_quote=str(equity) if equity is not None else None,
        )

    @protected.post("/pause")
    async def pause() -> CommandResponse:
        # Whole-bot commands on purpose: pause/resume/kill are operator
        # actions, and "I paused it" must never mean "except that symbol".
        for engine in state.engines.values():
            engine.pause()
        return CommandResponse(paused=True, detail="strategies muted; resting orders stay live")

    @protected.post("/resume")
    async def resume() -> CommandResponse:
        for engine in state.engines.values():
            engine.resume()
        return CommandResponse(paused=False, detail="strategies resumed")

    @protected.post("/kill")
    async def kill() -> CommandResponse:
        exits_submitted = 0
        failures: list[str] = []
        # Kill every engine before reporting any failure: one symbol's
        # unpriceable exit must not leave the others trading.
        for engine in state.engines.values():
            try:
                if await engine.kill():
                    exits_submitted += 1
            except RuntimeError as error:
                failures.append(str(error))
        if failures:
            # Halted but NOT flat — surface it as a clear conflict, never as
            # a 500 and never as a misleading "nothing to flatten". The
            # successes are reported too: "it failed" must not hide that
            # some exits *were* submitted.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"halted with failures ({exits_submitted} exit order(s) submitted): "
                    + "; ".join(failures)
                ),
            )
        plural = "s" if exits_submitted != 1 else ""
        detail = (
            f"halted; {exits_submitted} exit order{plural} submitted, fills on next candle"
            if exits_submitted
            else "halted; no position to flatten"
        )
        return CommandResponse(paused=True, detail=detail)

    @protected.post("/coins")
    async def add_coin(request: CoinActionRequest) -> CommandResponse:
        """Start trading a coin at runtime; persists across restarts."""
        try:
            await state.add_coin(request.symbol)
        except ValueError as error:
            # Bad pair, wrong quote currency, duplicate, or unlisted on the
            # exchange — the caller's input, so 400, with the reason verbatim.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(paused=first_engine.paused, detail=f"{request.symbol.strip()} added")

    @protected.post("/coins/remove")
    async def remove_coin(request: CoinActionRequest) -> CommandResponse:
        """Stop trading a coin; its candles, fills, and decisions stay queryable."""
        try:
            await state.remove_coin(request.symbol)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except RuntimeError as error:
            # Open position, pending proposal, or last coin: truthful
            # conflict — the operator must act first, not be silently obeyed.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(
            paused=first_engine.paused, detail=f"{request.symbol.strip()} removed"
        )

    @protected.post("/breakers/reset")
    async def reset_breakers() -> CommandResponse:
        """Clear a tripped circuit breaker — the explicit human reset.

        Deliberately does not resume a paused engine or forget the equity
        peak: it re-permits entries, nothing more. The breakers are one
        account-level instance shared by every engine, so resetting through
        any engine resets them all.
        """
        first_engine = next(iter(state.engines.values()))
        first_engine.reset_breakers()
        return CommandResponse(paused=first_engine.paused, detail="circuit breakers reset")

    def engine_for_proposal(signal_id: str) -> TradingEngine:
        """Route a proposal action to the engine whose queue knows the id."""
        for engine in state.engines.values():
            if engine.has_proposal(signal_id):
                return engine
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no pending proposal {signal_id!r}",
        )

    @protected.get("/proposals")
    async def get_proposals() -> list[ProposalResponse]:
        return [
            ProposalResponse(
                signal_id=proposal.signal.signal_id,
                symbol=proposal.signal.symbol,
                side=proposal.signal.side.value,
                strategy_name=proposal.signal.strategy_name,
                proposal_price_quote=str(proposal.proposal_price_quote),
                stop_price_quote=str(proposal.signal.stop_price_quote),
                reasons=list(proposal.signal.reasons),
                created_at=proposal.created_at.isoformat(),
                expires_at=proposal.expires_at.isoformat(),
            )
            for engine in state.engines.values()
            for proposal in engine.pending_proposals()
        ]

    @protected.post("/proposals/approve")
    async def approve_proposal(request: ProposalActionRequest) -> CommandResponse:
        engine = engine_for_proposal(request.signal_id)
        try:
            detail = await engine.approve_proposal(request.signal_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            # Expired or drifted: the yes was given to a market that no
            # longer exists, so the approval is refused — loudly.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CommandResponse(paused=engine.paused, detail=detail)

    @protected.post("/proposals/reject")
    async def reject_proposal(request: ProposalActionRequest) -> CommandResponse:
        engine = engine_for_proposal(request.signal_id)
        try:
            await engine.reject_proposal(request.signal_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            # Already resolved (expired/drifted/answered): truthful conflict,
            # not a 500 and not a misleading "not found".
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return CommandResponse(paused=engine.paused, detail="proposal rejected")

    @protected.get("/candles")
    async def get_candles(
        limit: int = Query(300, ge=1, le=1000),
        symbol: str | None = Query(None),
    ) -> list[CandleResponse]:
        candles = await state.candle_store.fetch_recent(
            resolve_symbol(symbol), CandleInterval.M1, limit
        )
        return [_candle_response(candle) for candle in candles]

    @protected.get("/decisions")
    async def get_decisions(
        limit: int = Query(50, ge=1, le=200),
        symbol: str | None = Query(None),
    ) -> list[DecisionResponse]:
        decisions = await state.decision_store.fetch_recent(resolve_symbol(symbol), limit)
        return [
            DecisionResponse(
                signal_id=decision.signal_id,
                strategy_name=decision.strategy_name,
                symbol=decision.symbol,
                side=decision.side.value,
                stop_price_quote=str(decision.stop_price_quote),
                reasons=list(decision.reasons),
                outcome=decision.outcome.value,
                created_at=decision.created_at.isoformat(),
            )
            for decision in decisions
        ]

    @protected.post("/evaluations")
    async def start_evaluation(request: EvaluationStartRequest) -> EvaluationStartResponse:
        """Start a blind walk-forward evaluation run (one at a time)."""
        config = EvaluationRunConfig(
            symbols=tuple(request.symbols) if request.symbols else tuple(active_symbols()),
            timeframes=tuple(request.timeframes),
            history_days=request.history_days,
            scenario_count=request.scenario_count,
            lookback_candles=request.lookback_candles,
            horizon_candles=request.horizon_candles,
            seed=request.seed,
        )
        try:
            run_id = await state.start_evaluation(config)
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return EvaluationStartResponse(run_id=run_id, detail="evaluation started")

    @protected.get("/evaluations")
    async def list_evaluations() -> list[EvaluationRunResponse]:
        return [_run_response(run) for run in await state.evaluation_store.list_runs()]

    @protected.get("/evaluations/{run_id}")
    async def get_evaluation(run_id: int) -> EvaluationRunResponse:
        run = await state.evaluation_store.fetch_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        return _run_response(run)

    @protected.get("/evaluations/{run_id}/scenarios")
    async def list_evaluation_scenarios(run_id: int) -> list[ScenarioSummaryResponse]:
        """List the run's graded scenarios for the replay browser."""
        if await state.evaluation_store.fetch_run(run_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        rows = await state.evaluation_store.list_scenarios_with_results(run_id)
        return [_scenario_summary(row) for row in rows]

    @protected.get("/evaluations/scenarios/{scenario_id}")
    async def get_scenario_replay(scenario_id: int) -> ScenarioReplayResponse:
        """One scenario's blind window, revealed horizon, decision, and grade.

        Candles are rebuilt from the candle store through the same
        aggregation path the run used — scenarios reference candles, they
        never copy them (ARCHITECTURE.md section 12.4).
        """
        row = await state.evaluation_store.fetch_scenario_with_result(scenario_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no graded scenario {scenario_id}",
            )
        run = await state.evaluation_store.fetch_run(row["run_id"])
        assert run is not None  # scenarios carry a foreign key to their run
        # The horizon length lives only in the run's config snapshot — it is
        # a run-level constant, not a per-scenario column.
        horizon_candles = int(run["config"]["horizon_candles"])
        window, horizon = await load_replay(
            state.candle_store,
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            decision_time=row["decision_time"],
            lookback_candles=row["lookback_candles"],
            horizon_candles=horizon_candles,
        )
        return ScenarioReplayResponse(
            scenario=_scenario_summary(row),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            reasons=list(row["reasons"]),
            entry_price_quote=_optional_str(row["entry_price_quote"]),
            exit_price_quote=_optional_str(row["exit_price_quote"]),
            pnl_quote=_optional_str(row["pnl_quote"]),
            mfe_r=_optional_str(row["mfe_r"]),
            mae_r=_optional_str(row["mae_r"]),
            duration_candles=row["duration_candles"],
            stop_hit=row["stop_hit"],
            oracle_r=_optional_str(row["oracle_r"]),
            window=[_candle_response(candle) for candle in window],
            horizon=[_candle_response(candle) for candle in horizon],
        )

    @protected.get("/evaluations/{run_id}/findings")
    async def list_evaluation_findings(run_id: int) -> list[FindingResponse]:
        """List the run's mined mistake patterns, each awaiting accept/reject."""
        if await state.evaluation_store.fetch_run(run_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no evaluation run {run_id}"
            )
        findings = await state.evaluation_store.fetch_findings(run_id)
        return [_finding_response(finding_id, finding) for finding_id, finding in findings]

    async def decide_finding(finding_id: int, verdict: str) -> FindingResponse:
        """Apply the human verdict; first answer wins, repeats are conflicts."""
        finding = await state.evaluation_store.fetch_finding(finding_id)
        if finding is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no finding {finding_id}"
            )
        if finding.status != "proposed":
            # The verdict is part of the run's lineage (§12.5); silently
            # flipping it would rewrite history, so repeats are refused.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"finding {finding_id} is already {finding.status}",
            )
        await state.evaluation_store.set_finding_status(finding_id, verdict)
        return _finding_response(finding_id, finding.model_copy(update={"status": verdict}))

    @protected.post("/evaluations/findings/{finding_id}/accept")
    async def accept_finding(finding_id: int) -> FindingResponse:
        """Accept a finding — the human judgement, recorded for lineage.

        Accepting records the judgement and nothing else: strategy
        configuration is never touched by the evaluation system
        (ARCHITECTURE.md section 12).
        """
        return await decide_finding(finding_id, "accepted")

    @protected.post("/evaluations/findings/{finding_id}/reject")
    async def reject_finding(finding_id: int) -> FindingResponse:
        """Reject a finding; it stays on record with its verdict."""
        return await decide_finding(finding_id, "rejected")

    @protected.post("/evaluations/{run_id}/cancel")
    async def cancel_evaluation(run_id: int) -> CommandResponse:
        if not state.cancel_evaluation(run_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"evaluation run {run_id} is not in flight",
            )
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(paused=first_engine.paused, detail=f"run {run_id} cancelled")

    @protected.post("/sweeps")
    async def start_sweep(request: SweepStartRequest) -> EvaluationStartResponse:
        """Start a walk-forward parameter sweep (one at a time)."""
        candidates = (
            tuple(
                SweepCandidate(name=candidate.name, params=candidate.params)
                for candidate in request.candidates
            )
            if request.candidates
            else DEFAULT_TREND_CANDIDATES
        )
        try:
            config = SweepConfig(
                symbol=request.symbol if request.symbol else resolve_symbol(None),
                timeframe=request.timeframe,
                history_days=request.history_days,
                scenario_count=request.scenario_count,
                lookback_candles=request.lookback_candles,
                horizon_candles=request.horizon_candles,
                seed=request.seed,
                training_fraction=request.training_fraction,
                candidates=candidates,
                motivating_finding_ids=tuple(request.motivating_finding_ids),
            )
            sweep_id = await state.start_sweep(config)
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            # Bad timeframe, duplicate candidate names, out-of-range split —
            # all caller input (ValidationError is a ValueError).
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return EvaluationStartResponse(run_id=sweep_id, detail="sweep started")

    @protected.get("/sweeps")
    async def list_sweeps() -> list[SweepResponse]:
        return [_sweep_response(sweep) for sweep in await state.evaluation_store.list_sweeps()]

    @protected.get("/sweeps/{sweep_id}")
    async def get_sweep(sweep_id: int) -> SweepResponse:
        sweep = await state.evaluation_store.fetch_sweep(sweep_id)
        if sweep is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no sweep {sweep_id}"
            )
        return _sweep_response(sweep)

    @protected.post("/sweeps/{sweep_id}/cancel")
    async def cancel_sweep(sweep_id: int) -> CommandResponse:
        if not state.cancel_sweep(sweep_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"sweep {sweep_id} is not in flight",
            )
        first_engine = next(iter(state.engines.values()))
        return CommandResponse(paused=first_engine.paused, detail=f"sweep {sweep_id} cancelled")

    @protected.get("/metrics")
    async def get_metrics() -> PlainTextResponse:
        """Prometheus text exposition (ARCHITECTURE.md 4.9).

        Behind the bearer token on purpose: balances and positions are in
        here, and Prometheus scrapes support bearer auth natively. Floats
        are fine in this one place — metrics are display, not accounting.
        """
        now = utc_now()
        portfolio = state.portfolio
        lines: list[str] = [
            "# TYPE tradebot_up gauge",
            format_metric("tradebot_up", 1),
            "# TYPE tradebot_quote_balance gauge",
            format_metric("tradebot_quote_balance", float(portfolio.quote_balance)),
            "# TYPE tradebot_realized_pnl_quote gauge",
            format_metric("tradebot_realized_pnl_quote", float(portfolio.realized_pnl_quote())),
            "# TYPE tradebot_open_positions gauge",
            format_metric("tradebot_open_positions", len(portfolio.positions)),
        ]
        lines.append("# TYPE tradebot_engine_paused gauge")
        for symbol, engine in state.engines.items():
            lines.append(
                format_metric("tradebot_engine_paused", int(engine.paused), {"symbol": symbol})
            )
        first_engine = next(iter(state.engines.values()), None)
        if first_engine is not None:
            lines.append("# TYPE tradebot_breaker_tripped gauge")
            lines.append(
                format_metric(
                    "tradebot_breaker_tripped",
                    int(first_engine.breakers.tripped_reason is not None),
                )
            )
        # Data-feed lag per symbol: the staleness alarm §4.9 asks for.
        lines.append("# TYPE tradebot_last_candle_age_seconds gauge")
        for symbol in state.engines:
            latest = await state.candle_store.latest_candle(symbol, CandleInterval.M1)
            if latest is not None:
                age = (now - latest.close_time).total_seconds()
                lines.append(
                    format_metric("tradebot_last_candle_age_seconds", age, {"symbol": symbol})
                )
        lines.append("# TYPE tradebot_news_flags_active gauge")
        lines.append(
            format_metric("tradebot_news_flags_active", len(state.news_flags.active_flags(now)))
        )
        lines.extend(state.metrics.render_counters())
        return PlainTextResponse("\n".join(lines) + "\n")

    @protected.get("/fills")
    async def get_fills(symbol: str | None = Query(None)) -> list[FillResponse]:
        # The journal view spans the whole account by default. Any symbol
        # may narrow it — including ones no longer configured: fills are
        # history, and history must stay queryable after a coin is removed.
        fills = await state.fill_store.fetch_all(symbol)
        return [
            FillResponse(
                client_order_id=fill.client_order_id,
                symbol=fill.symbol,
                side=fill.side.value,
                price_quote=str(fill.price_quote),
                quantity_base=str(fill.quantity_base),
                fee_quote=str(fill.fee_quote),
                filled_at=fill.filled_at.isoformat(),
            )
            for fill in fills
        ]

    app.include_router(protected)
    return app
