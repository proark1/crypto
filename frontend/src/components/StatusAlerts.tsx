/**
 * Renders the bot's blocking/pausing conditions as a consistent stack of
 * alert banners. Backed by `deriveStatusAlerts` so the status card and the
 * dashboard show the exact same set, each using the shared Alert primitive
 * (colour paired with an icon). Renders nothing when all is well.
 */
import type { StatusResponse } from "../api/types";
import { deriveStatusAlerts } from "../lib/statusAlerts";
import { Alert } from "../ui";

export function StatusAlerts(props: { status: StatusResponse; className?: string }) {
  const alerts = deriveStatusAlerts(props.status);
  if (alerts.length === 0) {
    return null;
  }
  return (
    <div className={`space-y-2 ${props.className ?? ""}`}>
      {alerts.map((alert) => (
        <Alert key={alert.id} tone={alert.tone} title={alert.title}>
          {alert.body}
        </Alert>
      ))}
    </div>
  );
}
