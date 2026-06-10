/**
 * The only place the frontend talks to the backend (CLAUDE.md: no ad hoc
 * fetch calls in components). Throws ApiError with the HTTP status so
 * screens can distinguish auth failures (prompt for token) from outages.
 */

import type { CommandResponse, DecisionResponse, FillResponse, StatusResponse } from "./types";

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

async function request<T>(path: string, method: "GET" | "POST"): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    method,
    headers: { Authorization: `Bearer ${getStoredToken()}` },
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

export function postPause(): Promise<CommandResponse> {
  return request<CommandResponse>("/pause", "POST");
}

export function postResume(): Promise<CommandResponse> {
  return request<CommandResponse>("/resume", "POST");
}

export function postKill(): Promise<CommandResponse> {
  return request<CommandResponse>("/kill", "POST");
}
