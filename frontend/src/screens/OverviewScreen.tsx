import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  fetchFills,
  fetchStatus,
  getStoredToken,
  postKill,
  postPause,
  postResume,
  storeToken,
} from "../api/client";
import type { FillResponse, StatusResponse } from "../api/types";
import { Controls } from "../components/Controls";
import { FillsTable } from "../components/FillsTable";
import { StatusCard } from "../components/StatusCard";

const POLL_INTERVAL_MS = 5000;

export function OverviewScreen() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [fills, setFills] = useState<FillResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(getStoredToken() === "");
  const [tokenDraft, setTokenDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextFills] = await Promise.all([fetchStatus(), fetchFills()]);
      setStatus(nextStatus);
      setFills(nextFills);
      setError(null);
      setNeedsToken(false);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setNeedsToken(true);
      } else {
        setError(caught instanceof Error ? caught.message : "request failed");
      }
    }
  }, []);

  useEffect(() => {
    if (needsToken) {
      return;
    }
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      clearInterval(timer);
    };
  }, [refresh, needsToken]);

  const runCommand = useCallback(
    async (command: () => Promise<{ detail: string }>) => {
      try {
        const result = await command();
        setNotice(result.detail);
        await refresh();
      } catch (caught) {
        setNotice(caught instanceof Error ? caught.message : "command failed");
      }
    },
    [refresh],
  );

  if (needsToken) {
    return (
      <div className="mx-auto mt-24 max-w-sm rounded-xl border border-zinc-800 bg-zinc-900 p-6">
        <h2 className="mb-2 text-lg font-bold text-zinc-100">API token</h2>
        <p className="mb-4 text-sm text-zinc-400">
          Paste the control-plane bearer token (TRADEBOT_API_TOKEN).
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            storeToken(tokenDraft.trim());
            setNeedsToken(false);
          }}
        >
          <input
            type="password"
            value={tokenDraft}
            onChange={(event) => {
              setTokenDraft(event.target.value);
            }}
            className="mb-3 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100"
            placeholder="token"
          />
          <button
            type="submit"
            className="w-full rounded-lg bg-emerald-600 px-4 py-2 font-semibold text-white hover:bg-emerald-500"
          >
            connect
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-zinc-100">tradebot</h1>
        {status && (
          <Controls
            paused={status.paused}
            onPause={() => void runCommand(postPause)}
            onResume={() => void runCommand(postResume)}
            onKill={() => void runCommand(postKill)}
          />
        )}
      </header>
      {notice && (
        <div className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-300">
          {notice}
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-red-800 bg-red-950/50 px-4 py-2 text-sm text-red-300">
          {error}
        </div>
      )}
      {status ? (
        <StatusCard status={status} />
      ) : (
        <div className="text-sm text-zinc-500">loading…</div>
      )}
      <FillsTable fills={fills} />
    </div>
  );
}
