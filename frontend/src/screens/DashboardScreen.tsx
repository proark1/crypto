/**
 * The landing tab: a short, glanceable answer to "how am I doing and does
 * anything need me?" — portfolio headline, any blocking conditions, the
 * co-pilot approvals inbox (promoted here because it is time-sensitive), a
 * compact leaderboard, and the wallet. The dense per-coin and per-bot detail
 * lives on the Coins and Bots tabs; this screen deliberately stays brief.
 */
import type {
  CompetitionResponse,
  ProposalResponse,
  StatusResponse,
  WalletResponse,
} from "../api/types";
import { MiniLeaderboard } from "../components/MiniLeaderboard";
import { PortfolioSummary } from "../components/PortfolioSummary";
import { ProposalsPanel } from "../components/ProposalsPanel";
import { StatusAlerts } from "../components/StatusAlerts";
import { WalletCard } from "../components/WalletCard";

export function DashboardScreen(props: {
  status: StatusResponse | null;
  competition: CompetitionResponse | null;
  wallet: WalletResponse | null;
  proposals: ProposalResponse[];
  disabled: boolean;
  onApprove: (signalId: string) => void;
  onReject: (signalId: string) => void;
  onViewAllBots: () => void;
  onSelectBot: (botId: string) => void;
}) {
  return (
    <div className="space-y-4">
      <PortfolioSummary status={props.status} competition={props.competition} />
      {props.status && <StatusAlerts status={props.status} />}
      <ProposalsPanel
        proposals={props.proposals}
        disabled={props.disabled}
        onApprove={props.onApprove}
        onReject={props.onReject}
      />
      <MiniLeaderboard
        competition={props.competition}
        onViewAll={props.onViewAllBots}
        onSelectBot={props.onSelectBot}
      />
      <WalletCard wallet={props.wallet} />
    </div>
  );
}
