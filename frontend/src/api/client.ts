/**
 * The only place the frontend talks to the backend (CLAUDE.md: no ad hoc
 * fetch calls in components). Throws ApiError with the HTTP status so
 * screens can distinguish auth failures (prompt for token) from outages.
 */

import type {
  CandleResponse,
  CommandResponse,
  DecisionResponse,
  FillResponse,
  ProposalResponse,
  StatusResponse,
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

export function fetchStatus(): Promise<StatusResponse> {
  return request<StatusResponse>("/status", "GET");
}

export function fetchFills(): Promise<FillResponse[]> {
  return request<FillResponse[]>("/fills", "GET");
}

export function fetchDecisions(): Promise<DecisionResponse[]> {
  return request<DecisionResponse[]>("/decisions", "GET");
}

export function fetchCandles(): Promise<CandleResponse[]> {
  return request<CandleResponse[]>("/candles", "GET");
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

export function approveProposal(signalId: string): Promise<CommandResponse> {
  return request<CommandResponse>("/proposals/approve", "POST", { signal_id: signalId });
}

export function rejectProposal(signalId: string): Promise<CommandResponse> {
  return request<CommandResponse>("/proposals/reject", "POST", { signal_id: signalId });
}
