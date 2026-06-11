import { useCallback, useEffect, useRef, useState } from "react";

import {
  addCoin,
  ApiError,
  approveProposal,
  fetchCandles,
  fetchCompetition,
  fetchDecisions,
  fetchFills,
  fetchProposals,
  fetchStatus,
  fetchWallet,
  getStoredToken,
  killBot,
  pauseBot,
  postKill,
  postPause,
  postResume,
  rejectProposal,
  removeCoin,
  resumeBot,
  storeToken,
} from "../api/client";
import type {
  CandleResponse,
  ChartInterval,
  CompetitionResponse,
  DecisionResponse,
  FillResponse,
  ProposalResponse,
  StatusResponse,
  WalletResponse,
} from "../api/types";
import { CandleChart } from "../components/CandleChart";
import { IntervalSwitcher } from "../components/IntervalSwitcher";
import { toTradeMarkers } from "../lib/chart";
import type { Theme } from "../lib/theme";
import { BotBuilderScreen } from "./BotBuilderScreen";
import { BotDetailScreen } from "./BotDetailScreen";
import { ResearchScreen } from "./ResearchScreen";
import { CoinManager } from "../components/CoinManager";
import { CompetitionCard } from "../components/CompetitionCard";
import { CoinTabs } from "../components/CoinTabs";
import { Controls } from "../components/Controls";
import { DecisionsPanel } from "../components/DecisionsPanel";
import { FillsTable } from "../components/FillsTable";
import { ProposalsPanel } from "../components/ProposalsPanel";
import { StatusCard } from "../components/StatusCard";
import { WalletCard } from "../components/WalletCard";

const POLL_INTERVAL_MS = 5000;

/** Client-side navigation: there is no router, so the visible screen is
 * plain state — the two tabs plus the bot detail and bot builder pages,
 * each with an explicit way back. */
type View =
  | { name: "overview" }
  | { name: "research" }
  | { name: "bot"; botId: string }
  | { name: "builder"; editBotId: string | null };

function SunIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

export function OverviewScreen(props: { theme: Theme; onToggleTheme: () => void }) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [fills, setFills] = useState<FillResponse[]>([]);
  const [decisions, setDecisions] = useState<DecisionResponse[]>([]);
  const [candles, setCandles] = useState<CandleResponse[]>([]);
  const [proposals, setProposals] = useState<ProposalResponse[]>([]);
  const [wallet, setWallet] = useState<WalletResponse | null>(null);
  const [competition, setCompetition] = useState<CompetitionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(getStoredToken() === "");
  const [tokenDraft, setTokenDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [commandPending, setCommandPending] = useState(false);
  // null until the first status arrives: the backend's first configured
  // coin is the default, and the frontend must not hardcode one.
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [chartInterval, setChartInterval] = useState<ChartInterval>("1m");
  const [view, setView] = useState<View>({ name: "overview" });
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    // Slow polls can resolve out of order; only the newest request may
    // touch state, so stale data never overwrites fresh data.
    const requestId = ++requestIdRef.current;
    const symbol = selectedSymbol ?? undefined;
    try {
      // Decisions are explainability, not safety: their endpoint failing
      // must never take down status/fills or the kill switch with it.
      const [
        nextStatus,
        nextFills,
        nextDecisions,
        nextCandles,
        nextProposals,
        nextWallet,
        nextCompetition,
      ] = await Promise.all([
        fetchStatus(symbol),
        fetchFills(),
        fetchDecisions(symbol).catch(() => null),
        fetchCandles(symbol, chartInterval).catch(() => null),
        fetchProposals().catch(() => null),
        fetchWallet().catch(() => null),
        fetchCompetition().catch(() => null),
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
      if (nextWallet !== null) {
        setWallet(nextWallet);
      }
      if (nextCompetition !== null) {
        setCompetition(nextCompetition);
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
  }, [selectedSymbol, chartInterval]);

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
      <div className="mx-auto mt-24 max-w-sm rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <h2 className="mb-2 text-lg font-bold text-zinc-900 dark:text-zinc-100">API token</h2>
        <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
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
            className="mb-3 w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
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

  // The bot pages live under the overview tab; keep it highlighted there
  // so the way back is always visible.
  const activeTab = view.name === "research" ? "research" : "overview";

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center justify-between gap-4 sm:justify-start">
          <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-100">
            tradebot
          </h1>
          <nav className="flex gap-1 rounded-lg bg-zinc-200/60 p-1 dark:bg-zinc-900">
            {(["overview", "research"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                onClick={() => {
                  setView(tab === "overview" ? { name: "overview" } : { name: "research" });
                }}
                className={`rounded-md px-3 py-1.5 text-sm font-semibold ${
                  activeTab === tab
                    ? "bg-white text-zinc-900 shadow-sm dark:bg-zinc-700 dark:text-zinc-100"
                    : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-200"
                }`}
              >
                {tab}
              </button>
            ))}
          </nav>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {view.name === "overview" && status && (
            <Controls
              paused={status.paused}
              disabled={commandPending}
              onPause={() => void runCommand(postPause)}
              onResume={() => void runCommand(postResume)}
              onKill={() => void runCommand(postKill)}
            />
          )}
          <button
            type="button"
            onClick={props.onToggleTheme}
            title={props.theme === "dark" ? "switch to light mode" : "switch to dark mode"}
            aria-label={props.theme === "dark" ? "switch to light mode" : "switch to dark mode"}
            className="ml-auto rounded-lg border border-zinc-200 bg-white p-2 text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            {props.theme === "dark" ? <SunIcon /> : <MoonIcon />}
          </button>
          <button
            type="button"
            onClick={() => {
              // Clear the stored token (LIVE_TRADING_CHECKLIST §8): a
              // shared or public browser must not keep control of the bot.
              storeToken("");
              setTokenDraft("");
              setNeedsToken(true);
            }}
            className="whitespace-nowrap rounded-lg border border-zinc-300 px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800"
          >
            log out
          </button>
        </div>
      </header>
      {notice && (
        <div className="rounded-lg border border-zinc-300 bg-white px-4 py-2 text-sm text-zinc-700 shadow-sm dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300">
          {notice}
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/50 dark:text-red-300">
          {error}
        </div>
      )}
      {view.name === "research" && <ResearchScreen />}
      {view.name === "bot" && (
        <BotDetailScreen
          botId={view.botId}
          symbol={status?.symbol ?? null}
          onBack={() => {
            setView({ name: "overview" });
          }}
          onEdit={(botId) => {
            setView({ name: "builder", editBotId: botId });
          }}
          onDeleted={() => {
            setView({ name: "overview" });
            void refresh();
          }}
        />
      )}
      {view.name === "builder" && (
        <BotBuilderScreen
          editBotId={view.editBotId}
          onCancel={() => {
            setView(
              view.editBotId === null
                ? { name: "overview" }
                : { name: "bot", botId: view.editBotId },
            );
          }}
          onSaved={(botId) => {
            setView({ name: "bot", botId });
            void refresh();
          }}
        />
      )}
      {view.name === "overview" && status && (
        <CoinTabs
          symbols={status.symbols}
          selected={status.symbol}
          disabled={commandPending}
          onSelect={setSelectedSymbol}
        />
      )}
      {view.name === "overview" && status && (
        <CoinManager
          selected={status.symbol}
          disabled={commandPending}
          onAdd={(symbol) => void runCommand(() => addCoin(symbol))}
          onRemove={(symbol) => void handleRemoveCoin(symbol)}
        />
      )}
      {view.name === "overview" &&
        (status ? (
          <StatusCard status={status} />
        ) : (
          <div className="text-sm text-zinc-500">loading…</div>
        ))}
      {view.name === "overview" && <WalletCard wallet={wallet} />}
      {view.name === "overview" && (
        <CompetitionCard
          competition={competition}
          disabled={commandPending}
          onSelectBot={(botId) => {
            setView({ name: "bot", botId });
          }}
          onCreateBot={() => {
            setView({ name: "builder", editBotId: null });
          }}
          onPauseBot={(botId) => void runCommand(() => pauseBot(botId))}
          onResumeBot={(botId) => void runCommand(() => resumeBot(botId))}
          onKillBot={(botId) => void runCommand(() => killBot(botId))}
        />
      )}
      {view.name === "overview" && (
        <>
          <ProposalsPanel
            proposals={proposals}
            disabled={commandPending}
            onApprove={(signalId) => void runCommand(() => approveProposal(signalId))}
            onReject={(signalId) => void runCommand(() => rejectProposal(signalId))}
          />
          <div className="flex justify-end">
            <IntervalSwitcher selected={chartInterval} onSelect={setChartInterval} />
          </div>
          <CandleChart
            candles={candles}
            // Markers must match the charted coin; the journal spans them all.
            markers={toTradeMarkers(
              status ? fills.filter((fill) => fill.symbol === status.symbol) : fills,
            )}
          />
          <DecisionsPanel decisions={decisions} />
          <FillsTable fills={fills} />
        </>
      )}
    </div>
  );
}
