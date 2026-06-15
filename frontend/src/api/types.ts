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
  /** Set only when a configured gate was switched off because its reference
   * market is not traded — the "entries run ungated" case. */
  reason: string | null;
}

export interface DataHealthResponse {
  /** False until the feed's first backfill confirms gap-free history, and
   * after any backfill fails; entries pause while degraded. */
  healthy: boolean;
  reason: string | null;
}

export interface StatusResponse {
  mode: string;
  paused: boolean;
  /** Armed protective stop level, or null while flat/unarmed. */
  protective_stop_quote: string | null;
  /** The regime gate's current verdict — first place to look when entries
   * keep showing up gated. */
  regime: RegimeResponse;
  /** The selected coin's market-data health; entries pause while degraded. */
  data_health: DataHealthResponse;
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
  /** Journal row id; opaque except as the `beforeId` cursor for older pages. */
  id: number;
  client_order_id: string;
  symbol: string;
  side: string;
  price_quote: string;
  quantity_base: string;
  /** Gross notional (price * quantity) in quote currency; fee excluded. */
  value_quote: string;
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
  /** How many earlier completed runs of the same bot mined this same
   * pattern — 0 means the pattern is new in this run. */
  seen_in_prior_runs: number;
  /** The earliest of those runs, for "recurred since run #N"; null when new. */
  first_seen_run_id: number | null;
  /** True while the run's accept-triggered coalescing timer is armed —
   * the verdict has been heard and a targeted sweep is about to start. */
  sweep_queued: boolean;
  /** The newest sweep this finding motivated (the card's cause-to-effect
   * chain: accepted → swept → verdict); null when none yet. */
  latest_sweep_id: number | null;
  latest_sweep_status: string | null;
  latest_sweep_verdict: string | null;
}

/** One research-timeline entry: server-composed prose plus the linkage and
 * tones the feed renders ("evaluation" | "sweep" | "promotion"). */
export interface TimelineEventResponse {
  at: string;
  kind: string;
  headline: string;
  detail: string | null;
  status: string | null;
  strategy: string | null;
  run_id: number | null;
  sweep_id: number | null;
  version_id: number | null;
  expectancy_r: string | null;
  verdict: string | null;
  /** Patterns mined in this run but absent from the same bot's previous
   * completed run (capped server-side). */
  new_patterns: string[];
  /** Patterns the previous run mined that this run no longer does. */
  resolved_patterns: string[];
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
  /** Bot id whose strategy was evaluated (e.g. "production", "breakout"). */
  strategy: string;
  /** Set when the run was part of a strategy-comparison batch, else null. */
  comparison_group: number | null;
}

/** Where a competing bot came from: the production regime router, a
 * built-in single-strategy challenger, or a user-built custom recipe. */
export type BotKind = "production" | "builtin" | "custom";

/** One bot an evaluation run can grade — the research bot selector's rows
 * (the fixed lineup plus any custom bots currently competing). */
export interface EvaluationStrategyResponse {
  id: string;
  label: string;
  description: string;
  kind: BotKind;
}

/** The automated improvement loop's schedule and latest outcome. A cycle is
 * in progress when last_cycle_started_at is newer than
 * last_cycle_finished_at; last_outcome is the loop's own plain-words line. */
export interface ImprovementStatusResponse {
  enabled: boolean;
  interval_hours: number;
  history_days: number;
  timeframe: string;
  last_cycle_started_at: string | null;
  last_cycle_finished_at: string | null;
  last_outcome: string | null;
  next_cycle_at: string | null;
}

/** One paper bot in the strategy competition: the production regime router,
 * a single-strategy challenger, or a custom bot — each its own account. */
export interface CompetitorResponse {
  bot_id: string;
  label: string;
  description: string;
  is_production: boolean;
  kind: BotKind;
  paused: boolean;
  equity_quote: string | null;
  initial_balance_quote: string;
  /** Return on the starting balance as a fraction (e.g. "0.0123" = +1.23%),
   * not a percent and not money — safe to parse for display only. */
  return_fraction: string | null;
  quote_balance: string;
  realized_pnl_quote: string;
  unrealized_pnl_quote: string | null;
  open_positions: number;
  entry_fills: number;
  exit_fills: number;
  breaker_tripped_reason: string | null;
}

export interface CompetitionResponse {
  quote_currency: string;
  /** Already ranked by the backend: best equity first, unknown equity last. */
  competitors: CompetitorResponse[];
}

/** Body for POST /evaluations/compare — every field optional; omitting all
 * of them runs the backend's default scenario set for every strategy. */
export interface ComparisonStartRequest {
  symbols?: string[];
  timeframes?: string[];
  history_days?: number;
  scenario_count?: number;
  lookback_candles?: number;
  horizon_candles?: number;
  seed?: number;
}

export interface ComparisonStartResponse {
  group_id: number;
  run_ids: number[];
  detail: string;
}

/** One comparison batch: the same scenarios graded once per strategy. */
export interface ComparisonGroupResponse {
  group_id: number;
  created_at: string;
  /** Runs in lineup order (production first, then the challengers). */
  runs: EvaluationRunResponse[];
}

/** One experiment the AI research advisor proposes (§12.9). Advisory only:
 * `parameter_hint` is prose a human reads, never an applied configuration. */
export interface ResearchHypothesisResponse {
  title: string;
  family: string;
  rationale: string;
  parameter_hint: string;
}

/** The advisor's read of a run: a plain-language diagnosis plus experiments. */
export interface ResearchAdvice {
  diagnosis: string;
  hypotheses: ResearchHypothesisResponse[];
}

/** The advise endpoint's envelope: `advice` when the advisor ran, otherwise
 * `available: false` (it is off by default, unavailable, or declined) — the
 * caller treats that as "nothing to show", never as an error. */
export interface ResearchAdviceResponse {
  available: boolean;
  advice: ResearchAdvice | null;
}

export interface BakeOffStartResponse {
  job_id: number;
  cells_total: number;
  detail: string;
}

/** One contestant's standing across the grid, money kept as a string. */
export interface BakeOffRankingEntry {
  bot_id: string;
  /** Mean return fraction over the cells this bot could trade (e.g. "0.034"). */
  average_return_fraction: string;
  cells_scored: number;
  total_trades: number;
}

/** One grid cell's outcome: every contestant's result, or insufficient data. */
export interface BakeOffCellRecord {
  timeframe: string;
  history_days: number;
  comparison_group: number | null;
  /** "completed" or "insufficient_data" (too little history to trade). */
  status: string;
  /** bot_id -> that contestant's result in this cell; empty when infeasible. */
  results: Record<
    string,
    { return_fraction: string; net_pnl_quote: string; trade_count: number }
  >;
}

/** The bake-off's accumulated leaderboard and per-cell detail (null until
 * the first cell finishes). */
export interface BakeOffResults {
  ranking: BakeOffRankingEntry[];
  cells: BakeOffCellRecord[];
}

/** One bake-off job: the grid run, its progress, and the (live or final)
 * ranking. Mirrors the backend's ``bake_off_jobs`` row. */
export interface BakeOffJobResponse {
  id: number;
  created_at: string;
  updated_at: string;
  status: string;
  config: Record<string, unknown>;
  contestants: string[];
  cells_done: number;
  cells_total: number;
  results: BakeOffResults | null;
}

/** How a multi-rule custom bot combines its rules' buy signals. */
export type EntryMode = "any" | "all";

/** One pickable strategy family in the bot builder, with the complete
 * default parameter set the backend would use (JSON values, not money). */
export interface StrategyFamilyOption {
  family: string;
  label: string;
  description: string;
  defaults: Record<string, unknown>;
}

export interface BotOptionsResponse {
  families: StrategyFamilyOption[];
  entry_modes: EntryMode[];
}

/** A custom bot's recipe: chosen families with parameter overrides. The
 * backend normalizes omissions, so sending the full default set is fine. */
export interface CustomBotRules {
  entry_mode?: EntryMode;
  families: Record<string, Record<string, unknown>>;
}

/** One open position in a competing bot's paper account. */
export interface BotPositionResponse {
  symbol: string;
  quantity_base: string;
  average_entry_price_quote: string;
  mark_price_quote: string | null;
  unrealized_pnl_quote: string | null;
}

/** How a bot trades, discriminated on `kind` to match the backend shape. */
export type BotStrategyResponse =
  | {
      kind: "production";
      regime_routed: boolean;
      families: Record<string, Record<string, unknown>>;
    }
  | { kind: "builtin"; family: string; params: Record<string, unknown> }
  | {
      kind: "custom";
      rules: { entry_mode: EntryMode; families: Record<string, Record<string, unknown>> };
    };

export interface BotDetailResponse {
  summary: CompetitorResponse;
  positions: BotPositionResponse[];
  strategy: BotStrategyResponse;
}

export interface BotCreateRequest {
  name: string;
  description?: string;
  rules: CustomBotRules;
}

export interface BotCreateResponse {
  bot_id: string;
  detail: string;
}

/** The buy/sell trading fees applied to every live paper fill. ``*_percent``
 * is what the settings form shows and edits ("0.1" = 0.1%); ``*_bps`` is the
 * exact basis-point value, both Decimal-safe strings. */
export interface TradingFeesResponse {
  buy_fee_percent: string;
  sell_fee_percent: string;
  buy_fee_bps: string;
  sell_fee_bps: string;
}

/** The §12.7 campaign loop's on/off and budget, for the Settings tab. `enabled`
 * is the live runtime toggle (persisted, no redeploy); the budget fields are
 * read-only context for the switch. */
export interface CampaignSettingsResponse {
  enabled: boolean;
  max_rounds: number;
  max_hours: number;
  timeframe: string;
}

/** One round of a campaign: its step, sweep, verdict, and any promotion. */
export interface CampaignRoundResponse {
  index: number;
  scale: number;
  sweep_id: number | null;
  verdict: string | null;
  winner: string | null;
  promoted_version: number | null;
  note: string;
}

/** The non-gating holdout read: the campaign's net move on the reserved slice. */
export interface CampaignHoldoutReadResponse {
  judged: boolean;
  improved: boolean;
  explanation: string;
  start_expectancy_r: string | null;
  final_expectancy_r: string | null;
}

/** A running (or last-run) campaign: target, progress, the round trail, and the
 * holdout read. `status` is "running" while in flight. */
export interface CampaignSnapshotResponse {
  target: string;
  symbol: string;
  status: string;
  promotions: number;
  stop_reason: string | null;
  holdout_start: string | null;
  started_at: string | null;
  finished_at: string | null;
  holdout_read: CampaignHoldoutReadResponse | null;
  rounds: CampaignRoundResponse[];
}

/** The §12.7 campaign loop: on/off, budget, and the current or last campaign. */
export interface CampaignStatusResponse {
  enabled: boolean;
  max_rounds: number;
  max_hours: number;
  timeframe: string;
  campaign: CampaignSnapshotResponse | null;
}

/** One ready-to-run evaluation shape, fitted server-side to the coin's
 * stored history — submitted verbatim to startEvaluation on click. */
export interface SuggestedEvaluationResponse {
  symbol: string;
  timeframe: string;
  history_days: number;
  expected_candles: number;
  scenario_count: number;
  title: string;
  rationale: string;
}

/** One §13.7 routing condition: whether it is met, and a plain-words reason. */
export interface CandidacyConditionResponse {
  met: boolean;
  detail: string;
}

/** One research family's §13.7 routing-evidence verdict (flag, never flip). */
export interface RoutingCandidacyResponse {
  family: string;
  is_candidate: boolean;
  validated_edge: CandidacyConditionResponse;
  beats_incumbent: CandidacyConditionResponse;
  live_paper: CandidacyConditionResponse;
}
