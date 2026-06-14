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
import type { Theme } from "../lib/theme";
import { Alert } from "../ui";
import { CompetitionCard } from "../components/CompetitionCard";
import { Controls } from "../components/Controls";
import { StatusPill } from "../components/StatusPill";
import { BotBuilderScreen } from "./BotBuilderScreen";
import { BotDetailScreen } from "./BotDetailScreen";
import { CoinsScreen } from "./CoinsScreen";
import { DashboardScreen } from "./DashboardScreen";
import { ResearchScreen } from "./ResearchScreen";
import { SettingsScreen } from "./SettingsScreen";

const POLL_INTERVAL_MS = 5000;

/** The four top-level destinations. The bot detail and builder pages are
 * drill-downs that live under the Bots tab, each with an explicit way back. */
type Tab = "dashboard" | "coins" | "bots" | "research" | "settings";
const TABS: { tab: Tab; label: string }[] = [
  { tab: "dashboard", label: "Dashboard" },
  { tab: "coins", label: "Coins" },
  { tab: "bots", label: "Bots" },
  { tab: "research", label: "Research" },
  { tab: "settings", label: "Settings" },
];

/** Client-side navigation: there is no router, so the visible screen is plain
 * state — the four tabs plus the bot detail and builder drill-downs. */
type View =
  | { name: Tab }
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
  const [view, setView] = useState<View>({ name: "dashboard" });
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
    // Self-scheduling instead of setInterval: each poll waits for the previous
    // one to settle before the next is queued, so a slow backend can never
    // stack overlapping requests (the request-id guard above is the backstop).
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      await refresh();
      if (!cancelled) {
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
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
            aria-label="API token"
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

  // Drill-downs keep the Bots tab highlighted so the way back is always clear.
  const activeTab: Tab = view.name === "bot" || view.name === "builder" ? "bots" : view.name;

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold tracking-tight text-zinc-900 dark:text-zinc-100">
            tradebot
          </h1>
          <StatusPill status={status} />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {status && (
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
      <nav className="flex gap-1 overflow-x-auto rounded-lg bg-zinc-200/60 p-1 dark:bg-zinc-900">
        {TABS.map(({ tab, label }) => (
          <button
            key={tab}
            type="button"
            onClick={() => {
              setView({ name: tab });
            }}
            className={`whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-semibold ${
              activeTab === tab
                ? "bg-white text-zinc-900 shadow-sm dark:bg-zinc-700 dark:text-zinc-100"
                : "text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-200"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>
      {notice && <Alert tone="info">{notice}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      {view.name === "dashboard" && (
        <DashboardScreen
          status={status}
          competition={competition}
          wallet={wallet}
          proposals={proposals}
          disabled={commandPending}
          onApprove={(signalId) => void runCommand(() => approveProposal(signalId))}
          onReject={(signalId) => void runCommand(() => rejectProposal(signalId))}
          onViewAllBots={() => {
            setView({ name: "bots" });
          }}
          onSelectBot={(botId) => {
            setView({ name: "bot", botId });
          }}
        />
      )}
      {view.name === "coins" && (
        <CoinsScreen
          status={status}
          candles={candles}
          decisions={decisions}
          fills={fills}
          chartInterval={chartInterval}
          disabled={commandPending}
          onSelectSymbol={setSelectedSymbol}
          onSelectInterval={setChartInterval}
          onAddCoin={(symbol) => void runCommand(() => addCoin(symbol))}
          onRemoveCoin={(symbol) => void handleRemoveCoin(symbol)}
        />
      )}
      {view.name === "bots" && (
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
      {view.name === "research" && <ResearchScreen />}
      {view.name === "settings" && <SettingsScreen />}
      {view.name === "bot" && (
        <BotDetailScreen
          botId={view.botId}
          symbol={status?.symbol ?? null}
          onBack={() => {
            setView({ name: "bots" });
          }}
          onEdit={(botId) => {
            setView({ name: "builder", editBotId: botId });
          }}
          onDeleted={() => {
            setView({ name: "bots" });
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
                ? { name: "bots" }
                : { name: "bot", botId: view.editBotId },
            );
          }}
          onSaved={(botId) => {
            setView({ name: "bot", botId });
            void refresh();
          }}
        />
      )}
    </div>
  );
}
