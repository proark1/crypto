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
from typing import Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from tradebot.core.config import AppConfig
from tradebot.core.models import CandleInterval
from tradebot.engine import TradingEngine
from tradebot.persistence import CandleStore, DecisionStore, FillStore
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

    @property
    def decision_store(self) -> DecisionStore:
        """The explainability trail: every signal and its fate."""
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

    configured_symbols = list(state.config.symbol_list())

    def resolve_symbol(symbol: str | None) -> str:
        """Default to the first configured symbol; 404 unknown ones."""
        if symbol is None:
            return configured_symbols[0]
        if symbol not in state.engines:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown symbol {symbol!r}; configured: {configured_symbols}",
            )
        return symbol

    async def account_equity() -> Decimal | None:
        """Mark every open position at its latest stored close; ``None`` if any lacks one."""
        marks: dict[str, Decimal] = {}
        for open_symbol in state.portfolio.positions:
            candle = await state.candle_store.latest_candle(open_symbol, CandleInterval.M1)
            if candle is None:
                return None  # refuse to guess, never wrong
            marks[open_symbol] = candle.close_quote
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
            symbols=configured_symbols,
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
            # a 500 and never as a misleading "nothing to flatten".
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="; ".join(failures))
        plural = "s" if exits_submitted != 1 else ""
        detail = (
            f"halted; {exits_submitted} exit order{plural} submitted, fills on next candle"
            if exits_submitted
            else "halted; no position to flatten"
        )
        return CommandResponse(paused=True, detail=detail)

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
        return [
            CandleResponse(
                open_time=candle.open_time.isoformat(),
                open_quote=str(candle.open_quote),
                high_quote=str(candle.high_quote),
                low_quote=str(candle.low_quote),
                close_quote=str(candle.close_quote),
                volume_base=str(candle.volume_base),
            )
            for candle in candles
        ]

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

    @protected.get("/fills")
    async def get_fills(symbol: str | None = Query(None)) -> list[FillResponse]:
        # The journal view spans the whole account by default; pass
        # ``symbol`` to narrow it.
        fills = await state.fill_store.fetch_all(
            resolve_symbol(symbol) if symbol is not None else None
        )
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
