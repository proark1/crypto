import type { FindingResponse } from "../api/types";

/**
 * Mined mistake patterns from a run, each awaiting the human verdict.
 * Accept/reject is a recorded judgement, nothing more — the evaluation
 * system never changes trading rules itself (ARCHITECTURE.md §12), so the
 * buttons call the API and the run's lineage carries the answer.
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
        scenarios as evidence — click one to replay it. Accepting a finding records “I believe
        this”; rejecting records “noise”. Neither changes the bot by itself — accepted findings
        are your reasons to try a parameter sweep below. Confidence is sample size: low means
        few examples, treat with suspicion.
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
              {finding.status !== "proposed" && (
                <span
                  className={`rounded px-2 py-0.5 text-xs ${
                    finding.status === "accepted"
                      ? "bg-emerald-100 dark:bg-emerald-900/60 text-emerald-700 dark:text-emerald-300"
                      : "bg-zinc-100 dark:bg-zinc-800 text-zinc-500"
                  }`}
                >
                  {finding.status}
                </span>
              )}
            </div>
            <p className="mt-1 text-zinc-700 dark:text-zinc-300">{finding.suggestion}</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {finding.status === "proposed" && (
                <>
                  <button
                    type="button"
                    onClick={() => {
                      props.onAccept(finding.id);
                    }}
                    className="rounded-lg bg-emerald-600 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-500"
                  >
                    accept
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      props.onReject(finding.id);
                    }}
                    className="rounded-lg border border-zinc-300 dark:border-zinc-700 px-3 py-1 text-xs text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-800"
                  >
                    reject
                  </button>
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
