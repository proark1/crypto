/**
 * Mirror of the backend control-plane schemas (backend/src/tradebot/api/app.py).
 *
 * All monetary amounts are strings: they are Decimal on the backend, and this
 * frontend never does money arithmetic — only display formatting (CLAUDE.md).
 */

export interface PositionResponse {
  symbol: string;
  quantity_base: string;
  average_entry_price_quote: string;
  unrealized_pnl_quote: string | null;
}

export interface StatusResponse {
  mode: string;
  paused: boolean;
  symbol: string;
  exchange_id: string;
  quote_currency: string;
  quote_balance: string;
  realized_pnl_quote: string;
  position: PositionResponse | null;
  last_candle_close_time: string | null;
  mark_price_quote: string | null;
  equity_quote: string | null;
}

export interface FillResponse {
  client_order_id: string;
  symbol: string;
  side: string;
  price_quote: string;
  quantity_base: string;
  fee_quote: string;
  filled_at: string;
}

export interface CommandResponse {
  paused: boolean;
  detail: string;
}

export interface CandleResponse {
  open_time: string;
  open_quote: string;
  high_quote: string;
  low_quote: string;
  close_quote: string;
  volume_base: string;
}

export interface DecisionResponse {
  signal_id: string;
  strategy_name: string;
  symbol: string;
  side: string;
  stop_price_quote: string;
  reasons: string[];
  outcome: string;
  created_at: string;
}
