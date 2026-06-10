import { useCallback, useEffect, useRef, useState } from "react";

import {
  addCoin,
  ApiError,
  approveProposal,
  fetchCandles,
  fetchDecisions,
  fetchFills,
  fetchProposals,
  fetchStatus,
  getStoredToken,
  postKill,
  postPause,
  postResume,
  rejectProposal,
  removeCoin,
  storeToken,
} from "../api/client";
import type {
  CandleResponse,
  DecisionResponse,
  FillResponse,
  ProposalResponse,
  StatusResponse,
} from "../api/types";
import { CandleChart } from "../components/CandleChart";
import { ResearchScreen } from "./ResearchScreen";
import { CoinManager } from "../components/CoinManager";
import { CoinTabs } from "../components/CoinTabs";
import { Controls } from "../components/Controls";
import { DecisionsPanel } from "../components/DecisionsPanel";
import { FillsTable } from "../components/FillsTable";
import { ProposalsPanel } from "../components/ProposalsPanel";
import { StatusCard } from "../components/StatusCard";

const POLL_INTERVAL_MS = 5000;

export function OverviewScreen() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [fills, setFills] = useState<FillResponse[]>([]);
  const [decisions, setDecisions] = useState<DecisionResponse[]>([]);
  const [candles, setCandles] = useState<CandleResponse[]>([]);
  const [proposals, setProposals] = useState<ProposalResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(getStoredToken() === "");
  const [tokenDraft, setTokenDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [commandPending, setCommandPending] = useState(false);
  // null until the first status arrives: the backend's first configured
  // coin is the default, and the frontend must not hardcode one.
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [screen, setScreen] = useState<"trade" | "research">("trade");
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    // Slow polls can resolve out of order; only the newest request may
    // touch state, so stale data never overwrites fresh data.
    const requestId = ++requestIdRef.current;
    const symbol = selectedSymbol ?? undefined;
    try {
      // Decisions are explainability, not safety: their endpoint failing
      // must never take down status/fills or the kill switch with it.
      const [nextStatus, nextFills, nextDecisions, nextCandles, nextProposals] =
        await Promise.all([
          fetchStatus(symbol),
          fetchFills(),
          fetchDecisions(symbol).catch(() => null),
          fetchCandles(symbol).catch(() => null),
          fetchProposals().catch(() => null),
        ]);
      if (requestId !== requestIdRef.current) {
        return;
      }
      setStatus(nextStatus);
      setFills(nextFills);
      if (nextDecisions !== null) {
        setDecisions(nextDecisions);
      }
      if (nextCandles !== null) {
        setCandles(nextCandles);
      }
      if (nextProposals !== null) {
        setProposals(nextProposals);
      }
      setError(null);
      setNeedsToken(false);
    } catch (caught) {
      if (requestId !== requestIdRef.current) {
        return;
      }
      if (caught instanceof ApiError && caught.status === 401) {
        setNeedsToken(true);
      } else {
        setError(caught instanceof Error ? caught.message : "request failed");
      }
    }
  }, [selectedSymbol]);

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
      setCommandPending(true);
      try {
        const result = await command();
        setNotice(result.detail);
        await refresh();
      } catch (caught) {
        setNotice(caught instanceof Error ? caught.message : "command failed");
      } finally {
        setCommandPending(false);
      }
    },
    [refresh],
  );

  const handleRemoveCoin = useCallback(
    async (symbol: string) => {
      setCommandPending(true);
      try {
        const result = await removeCoin(symbol);
        setNotice(result.detail);
        // Fall back to the backend's default coin. When no fallback is
        // needed (already on the default), refresh directly — refreshing
        // with the just-removed coin selected would 404.
        if (selectedSymbol === null) {
          await refresh();
        } else {
          setSelectedSymbol(null);
        }
      } catch (caught) {
        setNotice(caught instanceof Error ? caught.message : "command failed");
      } finally {
        setCommandPending(false);
      }
    },
    [refresh, selectedSymbol],
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
        <div className="flex items-center gap-4">
          <h1 className="text-2xl font-bold text-zinc-100">tradebot</h1>
          <nav className="flex gap-1 rounded-lg bg-zinc-900 p-1">
            {(["trade", "research"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => {
                  setScreen(tab);
                }}
                className={`rounded-md px-3 py-1 text-sm font-semibold ${
                  screen === tab ? "bg-zinc-700 text-zinc-100" : "text-zinc-400"
                }`}
              >
                {tab}
              </button>
            ))}
          </nav>
        </div>
        {screen === "trade" && status && (
          <Controls
            paused={status.paused}
            disabled={commandPending}
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
      {screen === "research" && <ResearchScreen />}
      {screen === "trade" && status && (
        <CoinTabs
          symbols={status.symbols}
          selected={status.symbol}
          disabled={commandPending}
          onSelect={setSelectedSymbol}
        />
      )}
      {screen === "trade" && status && (
        <CoinManager
          selected={status.symbol}
          disabled={commandPending}
          onAdd={(symbol) => void runCommand(() => addCoin(symbol))}
          onRemove={(symbol) => void handleRemoveCoin(symbol)}
        />
      )}
      {screen === "trade" &&
        (status ? (
          <StatusCard status={status} />
        ) : (
          <div className="text-sm text-zinc-500">loading…</div>
        ))}
      {screen === "trade" && (
        <>
          <ProposalsPanel
            proposals={proposals}
            disabled={commandPending}
            onApprove={(signalId) => void runCommand(() => approveProposal(signalId))}
            onReject={(signalId) => void runCommand(() => rejectProposal(signalId))}
          />
          <CandleChart
            candles={candles}
            // Markers must match the charted coin; the journal spans them all.
            fills={status ? fills.filter((fill) => fill.symbol === status.symbol) : fills}
          />
          <DecisionsPanel decisions={decisions} />
          <FillsTable fills={fills} />
        </>
      )}
    </div>
  );
}
