"""FastAPI app factory for the control plane.

Amounts are serialized as strings (Decimal-safe — the frontend never does
money arithmetic on floats, CLAUDE.md frontend rules). The app depends on a
narrow ``BotState`` protocol rather than the worker class itself, so it can
be tested with any object exposing the same surface and never imports the
composition root.
"""

from __future__ import annotations

import secrets
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, status
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
    def engine(self) -> TradingEngine:
        """The trading loop, for pause/resume/kill commands."""
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


class StatusResponse(BaseModel):
    """The three-second answer to "is everything okay?"."""

    mode: str
    paused: bool
    symbol: str
    exchange_id: str
    quote_currency: str
    quote_balance: str
    realized_pnl_quote: str
    position: PositionResponse | None
    last_candle_close_time: str | None
    mark_price_quote: str | None
    equity_quote: str | None


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


class FillResponse(BaseModel):
    """One journaled fill, amounts as strings."""

    client_order_id: str
    symbol: str
    side: str
    price_quote: str
    quantity_base: str
    fee_quote: str
    filled_at: str


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

    app = FastAPI(title="tradebot control plane", dependencies=[Depends(require_token)])

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": state.config.mode.value, "symbol": state.config.symbol}

    @app.get("/status")
    async def get_status() -> StatusResponse:
        portfolio = state.portfolio
        symbol = state.config.symbol
        latest = await state.candle_store.latest_candle(symbol, CandleInterval.M1)
        position = portfolio.position(symbol)

        mark_price = latest.close_quote if latest is not None else None
        if mark_price is not None:
            equity = portfolio.equity_quote({symbol: mark_price})
        elif position is None:
            equity = portfolio.equity_quote({})
        else:
            equity = None  # open position but no mark price: refuse to guess
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
        return StatusResponse(
            mode=state.config.mode.value,
            paused=state.engine.paused,
            symbol=symbol,
            exchange_id=state.config.exchange_id,
            quote_currency=state.config.quote_currency,
            quote_balance=str(portfolio.quote_balance),
            realized_pnl_quote=str(portfolio.realized_pnl_quote()),
            position=position_response,
            last_candle_close_time=(latest.close_time.isoformat() if latest is not None else None),
            mark_price_quote=str(mark_price) if mark_price is not None else None,
            equity_quote=str(equity) if equity is not None else None,
        )

    @app.post("/pause")
    async def pause() -> CommandResponse:
        state.engine.pause()
        return CommandResponse(paused=True, detail="strategy muted; resting orders stay live")

    @app.post("/resume")
    async def resume() -> CommandResponse:
        state.engine.resume()
        return CommandResponse(paused=False, detail="strategy resumed")

    @app.post("/kill")
    async def kill() -> CommandResponse:
        try:
            exit_submitted = await state.engine.kill()
        except RuntimeError as error:
            # Halted but NOT flat — surface it as a clear conflict, never as
            # a 500 and never as a misleading "nothing to flatten".
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        detail = (
            "halted; exit order submitted, fills on next candle"
            if exit_submitted
            else "halted; no position to flatten"
        )
        return CommandResponse(paused=True, detail=detail)

    @app.get("/decisions")
    async def get_decisions(limit: int = 50) -> list[DecisionResponse]:
        decisions = await state.decision_store.fetch_recent(state.config.symbol, limit)
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

    @app.get("/fills")
    async def get_fills() -> list[FillResponse]:
        fills = await state.fill_store.fetch_all(state.config.symbol)
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

    return app
