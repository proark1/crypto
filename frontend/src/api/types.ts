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

export interface BreakersResponse {
  tripped_reason: string | null;
  cooldown_until: string | null;
  entries_today: number;
}

export interface RegimeResponse {
  enabled: boolean;
  symbol: string | null;
  /** "warming_up" | "trending" | "ranging" | "risk_off" when enabled. */
  label: string | null;
  reasons: string[];
}

export interface StatusResponse {
  mode: string;
  paused: boolean;
  /** Armed protective stop level, or null while flat/unarmed. */
  protective_stop_quote: string | null;
  /** The regime gate's current verdict — first place to look when entries
   * keep showing up gated. */
  regime: RegimeResponse;
  symbol: string;
  symbols: string[];
  exchange_id: string;
  quote_currency: string;
  quote_balance: string;
  realized_pnl_quote: string;
  position: PositionResponse | null;
  last_candle_close_time: string | null;
  mark_price_quote: string | null;
  equity_quote: string | null;
  breakers: BreakersResponse;
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

export interface DivergenceReportResponse {
  window_start: string;
  window_end: string;
  live_fill_count: number;
  replay_fill_count: number;
  matched_count: number;
  /** 0 = every fill matched both ways; 1 = nothing matched. */
  divergence_fraction: number;
  mismatches: string[];
}

export interface CommandResponse {
  paused: boolean;
  detail: string;
}

/** Chart timeframes the backend can serve (1m raw, the rest aggregated). */
export type ChartInterval = "1m" | "1h" | "1d" | "1w" | "1M";

export interface CandleResponse {
  open_time: string;
  open_quote: string;
  high_quote: string;
  low_quote: string;
  close_quote: string;
  volume_base: string;
}

export interface ProposalResponse {
  signal_id: string;
  symbol: string;
  side: string;
  strategy_name: string;
  proposal_price_quote: string;
  stop_price_quote: string;
  reasons: string[];
  created_at: string;
  expires_at: string;
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

export interface ScenarioSummaryResponse {
  scenario_id: number;
  run_id: number;
  symbol: string;
  timeframe: string;
  decision_time: string;
  scenario_class: string;
  trend: string;
  volatility: string;
  events: string[];
  decision: string;
  verdict: string;
  r_multiple: string | null;
  timing: string | null;
}

export interface ScenarioReplayResponse {
  scenario: ScenarioSummaryResponse;
  confidence: number | null;
  reasons: string[];
  entry_price_quote: string | null;
  exit_price_quote: string | null;
  pnl_quote: string | null;
  mfe_r: string | null;
  mae_r: string | null;
  duration_candles: number | null;
  stop_hit: boolean | null;
  oracle_r: string | null;
  /** The blind context the bot decided on; its last candle closes at the decision. */
  window: CandleResponse[];
  /** The future it was graded against, for the viewer to reveal step by step. */
  horizon: CandleResponse[];
}

export interface FindingResponse {
  id: number;
  run_id: number;
  pattern: string;
  evidence_scenario_ids: number[];
  affected_count: number;
  average_r_impact: string;
  suggestion: string;
  confidence: string;
  status: string;
  created_at: string;
}

export interface HoldingResponse {
  asset: string;
  /** The pair behind a coin holding; null for the quote currency itself. */
  symbol: string | null;
  quantity: string;
  mark_price_quote: string | null;
  value_quote: string | null;
  unrealized_pnl_quote: string | null;
}

export interface WalletResponse {
  quote_currency: string;
  equity_quote: string | null;
  holdings: HoldingResponse[];
}

export interface StrategyVersionResponse {
  id: number;
  family: string;
  params: Record<string, unknown>;
  source_sweep_id: number | null;
  note: string | null;
  activated_at: string;
}

export interface SweepResponse {
  id: number;
  created_at: string;
  status: string;
  symbol: string;
  timeframe: string;
  config: Record<string, unknown>;
  motivating_finding_ids: number[];
  report: Record<string, unknown> | null;
}

export interface EvaluationRunResponse {
  id: number;
  created_at: string;
  status: string;
  symbols: string[];
  timeframes: string[];
  progress_done: number;
  progress_total: number;
  config: Record<string, unknown>;
  summary: Record<string, unknown> | null;
}
