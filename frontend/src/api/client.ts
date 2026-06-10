/**
 * The only place the frontend talks to the backend (CLAUDE.md: no ad hoc
 * fetch calls in components). Throws ApiError with the HTTP status so
 * screens can distinguish auth failures (prompt for token) from outages.
 */

import type {
  CandleResponse,
  EvaluationRunResponse,
  CommandResponse,
  DecisionResponse,
  FillResponse,
  FindingResponse,
  ProposalResponse,
  ScenarioReplayResponse,
  ScenarioSummaryResponse,
  StatusResponse,
  SweepResponse,
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

async function request<T>(path: string, method: "GET" | "POST", body?: unknown): Promise<T> {
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

function withSymbol(path: string, symbol?: string): string {
  // No symbol means the backend's default (its first configured pair).
  return symbol === undefined ? path : `${path}?symbol=${encodeURIComponent(symbol)}`;
}

export function fetchStatus(symbol?: string): Promise<StatusResponse> {
  return request<StatusResponse>(withSymbol("/status", symbol), "GET");
}

export function fetchFills(): Promise<FillResponse[]> {
  // Deliberately account-wide: the journal spans every coin.
  return request<FillResponse[]>("/fills", "GET");
}

export function fetchDecisions(symbol?: string): Promise<DecisionResponse[]> {
  return request<DecisionResponse[]>(withSymbol("/decisions", symbol), "GET");
}

export function fetchCandles(symbol?: string): Promise<CandleResponse[]> {
  return request<CandleResponse[]>(withSymbol("/candles", symbol), "GET");
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
  timeframes: string[];
  history_days: number;
  scenario_count: number;
}): Promise<{ run_id: number; detail: string }> {
  return request<{ run_id: number; detail: string }>("/evaluations", "POST", body);
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

export function fetchSweeps(): Promise<SweepResponse[]> {
  return request<SweepResponse[]>("/sweeps", "GET");
}

export function startSweep(body: {
  timeframe: string;
  history_days: number;
}): Promise<{ run_id: number; detail: string }> {
  // Omitting candidates sweeps the backend's default grid for the strategy.
  return request<{ run_id: number; detail: string }>("/sweeps", "POST", body);
}

export function cancelSweep(sweepId: number): Promise<CommandResponse> {
  return request<CommandResponse>(`/sweeps/${String(sweepId)}/cancel`, "POST");
}
