/**
 * The only place the frontend talks to the backend (CLAUDE.md: no ad hoc
 * fetch calls in components). Throws ApiError with the HTTP status so
 * screens can distinguish auth failures (prompt for token) from outages.
 */

import type {
  BakeOffJobResponse,
  BakeOffStartResponse,
  BotCreateRequest,
  BotCreateResponse,
  BotDetailResponse,
  BotOptionsResponse,
  CandleResponse,
  ChartInterval,
  CustomBotRules,
  ComparisonGroupResponse,
  ComparisonStartRequest,
  ComparisonStartResponse,
  CompetitionResponse,
  EvaluationRunResponse,
  EvaluationStrategyResponse,
  ImprovementStatusResponse,
  CommandResponse,
  DecisionResponse,
  FillResponse,
  FindingResponse,
  ProposalResponse,
  ResearchAdviceResponse,
  ScenarioReplayResponse,
  ScenarioSummaryResponse,
  DivergenceReportResponse,
  RoutingCandidacyResponse,
  StatusResponse,
  StrategyVersionResponse,
  SuggestedEvaluationResponse,
  SweepResponse,
  TimelineEventResponse,
  TradingFeesResponse,
  WalletResponse,
} from "./types";

const TOKEN_STORAGE_KEY = "tradebot_api_token";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getStoredToken(): string {
  // localStorage can throw in restricted contexts (sandboxed iframes,
  // blocked storage); degrade to "no token" instead of crashing at load.
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function storeToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // storage unavailable: the session works until reload
  }
}

const BASE_URL: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

async function request<T>(
  path: string,
  method: "GET" | "POST" | "PUT" | "DELETE",
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${getStoredToken()}` };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // non-JSON error body: keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

/** Append the defined params as a query string; omitted params mean the
 * backend's defaults (e.g. no symbol = its first configured pair). */
function withQuery(path: string, params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      search.set(key, value);
    }
  }
  const query = search.toString();
  return query === "" ? path : `${path}?${query}`;
}

export function fetchStatus(symbol?: string): Promise<StatusResponse> {
  return request<StatusResponse>(withQuery("/status", { symbol }), "GET");
}

/** Live-vs-replay divergence (the paper-gate metric). Recomputed per call —
 * the backend replays the window — so poll sparingly. */
export function fetchDivergence(symbol: string, hours = 24): Promise<DivergenceReportResponse> {
  return request<DivergenceReportResponse>(
    `/coins/${symbol}/divergence?hours=${String(hours)}`,
    "GET",
  );
}

export function fetchWallet(): Promise<WalletResponse> {
  return request<WalletResponse>("/wallet", "GET");
}

/** The trade journal: account-wide by default (spans every coin), or one
 * competing bot's own journal when its bot_id is given. Bounded to the newest
 * `limit` fills; pass `beforeId` (the smallest id already seen) to page back
 * through older history. Each page arrives oldest-first. */
export function fetchFills(
  bot?: string,
  options?: { limit?: number; beforeId?: number },
): Promise<FillResponse[]> {
  return request<FillResponse[]>(
    withQuery("/fills", {
      bot,
      limit: options?.limit?.toString(),
      before_id: options?.beforeId?.toString(),
    }),
    "GET",
  );
}

export function fetchDecisions(symbol?: string, bot?: string): Promise<DecisionResponse[]> {
  return request<DecisionResponse[]>(withQuery("/decisions", { symbol, bot }), "GET");
}

export function fetchCandles(
  symbol?: string,
  interval: ChartInterval = "1m",
): Promise<CandleResponse[]> {
  return request<CandleResponse[]>(withQuery("/candles", { symbol, interval }), "GET");
}

export function postPause(): Promise<CommandResponse> {
  return request<CommandResponse>("/pause", "POST");
}

export function postResume(): Promise<CommandResponse> {
  return request<CommandResponse>("/resume", "POST");
}

export function postKill(): Promise<CommandResponse> {
  return request<CommandResponse>("/kill", "POST");
}

export function fetchProposals(): Promise<ProposalResponse[]> {
  return request<ProposalResponse[]>("/proposals", "GET");
}

export function addCoin(symbol: string): Promise<CommandResponse> {
  return request<CommandResponse>("/coins", "POST", { symbol });
}

export function removeCoin(symbol: string): Promise<CommandResponse> {
  return request<CommandResponse>("/coins/remove", "POST", { symbol });
}

export function approveProposal(signalId: string): Promise<CommandResponse> {
  return request<CommandResponse>("/proposals/approve", "POST", { signal_id: signalId });
}

export function rejectProposal(signalId: string): Promise<CommandResponse> {
  return request<CommandResponse>("/proposals/reject", "POST", { signal_id: signalId });
}

export function fetchEvaluations(): Promise<EvaluationRunResponse[]> {
  return request<EvaluationRunResponse[]>("/evaluations", "GET");
}

export function fetchEvaluation(runId: number): Promise<EvaluationRunResponse> {
  return request<EvaluationRunResponse>(`/evaluations/${String(runId)}`, "GET");
}

export function startEvaluation(body: {
  symbols?: string[];
  timeframes: string[];
  history_days: number;
  scenario_count: number;
  /** Which bot the run grades (see fetchEvaluationStrategies); omitting it
   * means the backend's default: production, the incumbent. */
  strategy?: string;
}): Promise<{ run_id: number; detail: string }> {
  // Omitting symbols means the backend's default: every active coin.
  return request<{ run_id: number; detail: string }>("/evaluations", "POST", body);
}

/** Every bot an evaluation run can grade: the fixed lineup plus custom
 * bots currently in the competition. Production leads the list. */
export function fetchEvaluationStrategies(): Promise<EvaluationStrategyResponse[]> {
  return request<EvaluationStrategyResponse[]>("/evaluations/strategies", "GET");
}

/** The automated improvement loop's schedule and latest outcome — always
 * answers, with enabled=false when the loop is off. */
export function fetchImprovementStatus(): Promise<ImprovementStatusResponse> {
  return request<ImprovementStatusResponse>("/improvement", "GET");
}

/** The strategy-competition leaderboard. Competitors arrive already ranked
 * best equity first (null equity last) — render them in order. */
export function fetchCompetition(): Promise<CompetitionResponse> {
  return request<CompetitionResponse>("/competition", "GET");
}

/** The rule cards the bot builder offers, with their default parameters. */
export function fetchBotOptions(): Promise<BotOptionsResponse> {
  return request<BotOptionsResponse>("/bots/options", "GET");
}

/** One competing bot in full: summary row, open positions, and how it
 * trades. 404s for unknown bot ids — the caller decides how to recover. */
export function fetchBot(botId: string): Promise<BotDetailResponse> {
  return request<BotDetailResponse>(`/bots/${encodeURIComponent(botId)}`, "GET");
}

/** Create a custom bot from a rule recipe. 400 = invalid recipe or
 * duplicate name, 409 = the competition is disabled — both in plain words. */
export function createBot(body: BotCreateRequest): Promise<BotCreateResponse> {
  return request<BotCreateResponse>("/bots", "POST", body);
}

export function updateBotRules(botId: string, rules: CustomBotRules): Promise<CommandResponse> {
  return request<CommandResponse>(`/bots/${encodeURIComponent(botId)}/rules`, "PUT", { rules });
}

/** Pause one competing bot only; its protective stops keep running. */
export function pauseBot(botId: string): Promise<CommandResponse> {
  return request<CommandResponse>(`/bots/${encodeURIComponent(botId)}/pause`, "POST");
}

export function resumeBot(botId: string): Promise<CommandResponse> {
  return request<CommandResponse>(`/bots/${encodeURIComponent(botId)}/resume`, "POST");
}

/** Stop & flatten one bot: halts it and sells its positions at market on
 * the next candle. 409 with a plain-words detail on partial failure. */
export function killBot(botId: string): Promise<CommandResponse> {
  return request<CommandResponse>(`/bots/${encodeURIComponent(botId)}/kill`, "POST");
}

/** Delete a custom bot. 400 for built-ins, 409 while it still holds a
 * position or orders ("stop the bot first"), 404 unknown. */
export function deleteBot(botId: string): Promise<CommandResponse> {
  return request<CommandResponse>(`/bots/${encodeURIComponent(botId)}`, "DELETE");
}

/** Reset a bot's paper account to a new starting capital (quote currency).
 * Destructive: purges the bot's journals and restarts it clean. 400 for a
 * negative amount, 404 unknown, 409 while the bot holds a position/orders. */
export function resetBotCapital(
  botId: string,
  initialBalanceQuote: string,
): Promise<CommandResponse> {
  return request<CommandResponse>(`/bots/${encodeURIComponent(botId)}/capital`, "PUT", {
    initial_balance_quote: initialBalanceQuote,
  });
}

/** The buy/sell trading fees applied to every live paper fill. */
export function fetchTradingFees(): Promise<TradingFeesResponse> {
  return request<TradingFeesResponse>("/settings/fees", "GET");
}

/** Set the buy/sell fees, as percentages of notional ("0.1" = 0.1%). Takes
 * effect on the next fill across every bot. 400 on a negative or absurd fee. */
export function updateTradingFees(
  buyFeePercent: string,
  sellFeePercent: string,
): Promise<TradingFeesResponse> {
  return request<TradingFeesResponse>("/settings/fees", "PUT", {
    buy_fee_percent: buyFeePercent,
    sell_fee_percent: sellFeePercent,
  });
}

/** Start one evaluation run per strategy over identical scenario sets.
 * An empty body means the backend's defaults; 409 = a batch or sweep is
 * already in flight, 400 = bad config — both carry a plain-words detail. */
export function startComparison(
  body: ComparisonStartRequest = {},
): Promise<ComparisonStartResponse> {
  return request<ComparisonStartResponse>("/evaluations/compare", "POST", body);
}

/** Past comparison batches, newest first, runs in lineup order. */
export function fetchComparisons(): Promise<ComparisonGroupResponse[]> {
  return request<ComparisonGroupResponse[]>("/evaluations/comparisons", "GET");
}

/** Start a bake-off: the contestant roster across the whole grid. An empty
 * body takes the backend's defaults (the live coins, the 3x3 grid); 409 = a
 * bake-off is already running, 400 = bad grid. */
export function startBakeOff(
  body: Record<string, unknown> = {},
): Promise<BakeOffStartResponse> {
  return request<BakeOffStartResponse>("/research/bakeoff", "POST", body);
}

/** Past bake-off jobs, newest first. */
export function fetchBakeOffs(): Promise<BakeOffJobResponse[]> {
  return request<BakeOffJobResponse[]>("/research/bakeoffs", "GET");
}

/** One bake-off job by id (status, progress, and the running ranking). */
export function fetchBakeOff(jobId: number): Promise<BakeOffJobResponse> {
  return request<BakeOffJobResponse>(`/research/bakeoff/${String(jobId)}`, "GET");
}

export function fetchEvaluationSuggestions(): Promise<SuggestedEvaluationResponse[]> {
  return request<SuggestedEvaluationResponse[]>("/evaluations/suggestions", "GET");
}

export function cancelEvaluation(runId: number): Promise<CommandResponse> {
  return request<CommandResponse>(`/evaluations/${String(runId)}/cancel`, "POST");
}

export function fetchScenarios(runId: number): Promise<ScenarioSummaryResponse[]> {
  return request<ScenarioSummaryResponse[]>(`/evaluations/${String(runId)}/scenarios`, "GET");
}

export function fetchScenarioReplay(scenarioId: number): Promise<ScenarioReplayResponse> {
  return request<ScenarioReplayResponse>(`/evaluations/scenarios/${String(scenarioId)}`, "GET");
}

export function fetchFindings(runId: number): Promise<FindingResponse[]> {
  return request<FindingResponse[]>(`/evaluations/${String(runId)}/findings`, "GET");
}

export function acceptFinding(findingId: number): Promise<FindingResponse> {
  return request<FindingResponse>(`/evaluations/findings/${String(findingId)}/accept`, "POST");
}

export function rejectFinding(findingId: number): Promise<FindingResponse> {
  return request<FindingResponse>(`/evaluations/findings/${String(findingId)}/reject`, "POST");
}

/** Ask the AI research advisor (§12.9) to read a completed run and propose
 * experiments. Advisory only: the envelope carries `available: false` (never an
 * error) when the advisor is disabled, unavailable, or declines. */
export function requestResearchAdvice(runId: number): Promise<ResearchAdviceResponse> {
  return request<ResearchAdviceResponse>(`/evaluations/${String(runId)}/advise`, "POST");
}

export function fetchStrategyVersions(): Promise<StrategyVersionResponse[]> {
  return request<StrategyVersionResponse[]>("/strategy/versions", "GET");
}

export function revertStrategyVersion(versionId: number): Promise<CommandResponse> {
  return request<CommandResponse>(`/strategy/versions/${String(versionId)}/revert`, "POST");
}

export function fetchSweeps(): Promise<SweepResponse[]> {
  return request<SweepResponse[]>("/sweeps", "GET");
}

export function startSweep(
  body: { timeframe?: string; history_days?: number } = {},
): Promise<{ run_id: number; detail: string }> {
  // Omitting fields lets the backend pick unstarved defaults (a year of
  // history, a scenario budget sized to clear the minimum-trades bar) and
  // derive the candidate grid: variants of the actively traded parameters
  // plus challengers targeted at the latest run's findings.
  return request<{ run_id: number; detail: string }>("/sweeps", "POST", body);
}

export function cancelSweep(sweepId: number): Promise<CommandResponse> {
  return request<CommandResponse>(`/sweeps/${String(sweepId)}/cancel`, "POST");
}

/** The research story, newest first: completed runs (with their finding
 * diffs), sweep verdicts, and settings promotions as one feed. */
export function fetchResearchTimeline(limit = 50): Promise<TimelineEventResponse[]> {
  return request<TimelineEventResponse[]>(`/research/timeline?limit=${String(limit)}`, "GET");
}

/** The §13.7 routing-evidence gate per research family — flag, never flip.
 * Whether each family has earned a validated edge in a regime, beaten the
 * incumbent across batches, and soaked positively in live paper for 8 weeks. */
export function fetchRoutingCandidacy(): Promise<RoutingCandidacyResponse[]> {
  return request<RoutingCandidacyResponse[]>("/research/candidacy", "GET");
}
