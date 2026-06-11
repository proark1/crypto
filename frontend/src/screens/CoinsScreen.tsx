/**
 * The per-coin trading detail, scoped to one coin at a time: pick the coin,
 * manage which coins trade, then read its live status, price chart, decision
 * journal, and fills. This is the old overview's trading section, lifted out
 * so it is not interleaved with portfolio- and bot-level content.
 */
import type {
  CandleResponse,
  ChartInterval,
  DecisionResponse,
  FillResponse,
  StatusResponse,
} from "../api/types";
import { CandleChart } from "../components/CandleChart";
import { CoinManager } from "../components/CoinManager";
import { CoinTabs } from "../components/CoinTabs";
import { DecisionsPanel } from "../components/DecisionsPanel";
import { FillsTable } from "../components/FillsTable";
import { IntervalSwitcher } from "../components/IntervalSwitcher";
import { StatusCard } from "../components/StatusCard";
import { toTradeMarkers } from "../lib/chart";

export function CoinsScreen(props: {
  status: StatusResponse | null;
  candles: CandleResponse[];
  decisions: DecisionResponse[];
  fills: FillResponse[];
  chartInterval: ChartInterval;
  disabled: boolean;
  onSelectSymbol: (symbol: string) => void;
  onSelectInterval: (interval: ChartInterval) => void;
  onAddCoin: (symbol: string) => void;
  onRemoveCoin: (symbol: string) => void;
}) {
  const { status, fills } = props;
  // Markers must match the charted coin; the journal spans them all.
  const markers = toTradeMarkers(
    status ? fills.filter((fill) => fill.symbol === status.symbol) : fills,
  );
  return (
    <div className="space-y-4">
      {status && (
        <>
          <CoinTabs
            symbols={status.symbols}
            selected={status.symbol}
            disabled={props.disabled}
            onSelect={props.onSelectSymbol}
          />
          <CoinManager
            selected={status.symbol}
            disabled={props.disabled}
            onAdd={props.onAddCoin}
            onRemove={props.onRemoveCoin}
          />
        </>
      )}
      {status ? (
        <StatusCard status={status} />
      ) : (
        <div className="text-sm text-zinc-500">loading…</div>
      )}
      <div className="flex justify-end">
        <IntervalSwitcher selected={props.chartInterval} onSelect={props.onSelectInterval} />
      </div>
      <CandleChart candles={props.candles} markers={markers} />
      <DecisionsPanel decisions={props.decisions} />
      <FillsTable fills={fills} />
    </div>
  );
}
