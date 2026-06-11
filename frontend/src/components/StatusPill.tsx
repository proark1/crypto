/**
 * The always-visible safety glance in the header. A trading dashboard must
 * never make the user scroll to learn whether it is live, paused, or blocked;
 * this pill carries the mode (paper vs live, the most consequential fact),
 * a paused marker, and — when something is wrong — an attention badge counting
 * the active blocking conditions. It pairs every state with an icon so the
 * meaning survives a glance or a grayscale view.
 */
import type { StatusResponse } from "../api/types";
import { deriveStatusAlerts } from "../lib/statusAlerts";
import { AlertTriangleIcon, Badge, PauseIcon } from "../ui";

export function StatusPill(props: { status: StatusResponse | null }) {
  if (props.status === null) {
    return null;
  }
  const { status } = props;
  const alerts = deriveStatusAlerts(status);
  const live = status.mode === "live";
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge
        tone={live ? "red" : "amber"}
        title={
          live ? "live — trading real money" : "practice mode — simulated money, real prices"
        }
      >
        {status.mode}
      </Badge>
      {status.paused && (
        <Badge
          tone="zinc"
          title="paused — not opening new positions"
          icon={<PauseIcon className="h-3 w-3" />}
        >
          paused
        </Badge>
      )}
      {alerts.length > 0 && (
        <Badge
          tone="red"
          title={alerts.map((alert) => alert.title).join("; ")}
          icon={<AlertTriangleIcon className="h-3 w-3" />}
        >
          {alerts.length === 1 ? "1 alert" : `${String(alerts.length)} alerts`}
        </Badge>
      )}
    </span>
  );
}
