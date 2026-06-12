import type { FindingResponse } from "../api/types";
import { Badge, Button, type BadgeTone } from "../ui";

/**
 * The cause-to-effect chain a verdict sets off: a queued coalescing timer,
 * the sweep it fires, and that sweep's verdict — read off the sweep's
 * recorded motivation, so the badge can never disagree with the journal.
 */
function SweepChainBadge(props: { finding: FindingResponse }) {
  const finding = props.finding;
  if (finding.sweep_queued && finding.latest_sweep_id === null) {
    return <Badge tone="amber">sweep queued</Badge>;
  }
  if (finding.latest_sweep_id === null) {
    return null;
  }
  if (finding.latest_sweep_status !== "completed") {
    const label =
      finding.latest_sweep_status === "running" || finding.latest_sweep_status === "pending"
        ? `sweeping #${String(finding.latest_sweep_id)}`
        : `sweep #${String(finding.latest_sweep_id)} ${finding.latest_sweep_status ?? ""}`;
    return <Badge tone="violet">{label}</Badge>;
  }
  const verdict = finding.latest_sweep_verdict ?? "no verdict";
  const tone: BadgeTone =
    verdict === "validated" ? "emerald" : verdict === "overfit" ? "red" : "zinc";
  return (
    <Badge tone={tone}>
      sweep #{String(finding.latest_sweep_id)}: {verdict.replace(/_/g, " ")}
    </Badge>
  );
}

/**
 * Mined mistake patterns from a run, each awaiting the human verdict.
 * Accepting queues a coalesced, findings-targeted parameter sweep (the
 * verdict becomes a test); the chain badge on each card follows it from
 * queued through sweeping to the sweep's verdict. Settings still only ever
 * change through a statistically validated sweep winner, on paper
 * (ARCHITECTURE.md §12.7) — the buttons themselves never touch the bot.
 */
export function FindingsPanel(props: {
  findings: FindingResponse[];
  onAccept: (findingId: number) => void;
  onReject: (findingId: number) => void;
  onReplayEvidence: (scenarioId: number) => void;
}) {
  if (props.findings.length === 0) {
    return null;
  }
  return (
    <div>
      <h4 className="text-xs uppercase tracking-wide text-zinc-500">
        findings — suggestions only; nothing changes without your verdict
      </h4>
      <p className="mb-2 mt-0.5 text-xs text-zinc-500">
        Each card is a money-losing (or money-missing) pattern mined from this run, with the
        scenarios as evidence — click one to replay it. Accepting records “I believe this” and
        queues a parameter sweep targeted at the accepted findings (nearby accepts share one
        sweep); rejecting records “noise” and steers future sweeps away. Settings only ever
        change when a sweep winner is statistically validated, and only on paper. Confidence is
        sample size: low means few examples, treat with suspicion.
      </p>
      <ul className="space-y-2">
        {props.findings.map((finding) => (
          <li
            key={finding.id}
            className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3 text-sm"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-zinc-900 dark:text-zinc-100">
                {finding.pattern}
              </span>
              <span className="rounded bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 text-xs text-zinc-600 dark:text-zinc-400">
                {finding.affected_count} scenarios · {finding.average_r_impact}R ·{" "}
                {finding.confidence} confidence
              </span>
              {finding.seen_in_prior_runs > 0 ? (
                <Badge tone="amber">
                  recurred · {finding.seen_in_prior_runs + 1} runs
                  {finding.first_seen_run_id !== null
                    ? ` since #${String(finding.first_seen_run_id)}`
                    : ""}
                </Badge>
              ) : (
                <Badge tone="sky">new pattern</Badge>
              )}
              {finding.status !== "proposed" && (
                <Badge tone={finding.status === "accepted" ? "emerald" : "zinc"}>
                  {finding.status}
                </Badge>
              )}
              <SweepChainBadge finding={finding} />
            </div>
            <p className="mt-1 text-zinc-700 dark:text-zinc-300">{finding.suggestion}</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {finding.status === "proposed" && (
                <>
                  <Button
                    size="sm"
                    onClick={() => {
                      props.onAccept(finding.id);
                    }}
                  >
                    accept
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      props.onReject(finding.id);
                    }}
                  >
                    reject
                  </Button>
                </>
              )}
              <span className="text-xs text-zinc-500">evidence:</span>
              {finding.evidence_scenario_ids.slice(0, 8).map((scenarioId) => (
                <button
                  key={scenarioId}
                  type="button"
                  onClick={() => {
                    props.onReplayEvidence(scenarioId);
                  }}
                  className="text-xs text-sky-600 dark:text-sky-400 hover:text-sky-500 dark:hover:text-sky-300"
                >
                  #{scenarioId}
                </button>
              ))}
              {finding.evidence_scenario_ids.length > 8 && (
                <span className="text-xs text-zinc-500">
                  +{finding.evidence_scenario_ids.length - 8} more
                </span>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
