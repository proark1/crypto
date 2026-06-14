import { useState } from "react";

import { requestResearchAdvice } from "../api/client";
import type { ResearchAdviceResponse } from "../api/types";
import { Button, Card } from "../ui";

/**
 * The AI research advisor (§12.9): on demand, asks a model to read this run's
 * report and mined findings and propose experiments worth trying. Advisory
 * only — every hypothesis is something a human chooses to run as a sweep;
 * nothing here changes the strategy or places an order. The backend has it off
 * by default, so when no advice comes back the panel says how to enable it
 * rather than looking broken.
 */
export function AdvisorPanel(props: { runId: number }) {
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [result, setResult] = useState<ResearchAdviceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = () => {
    setStatus("loading");
    setError(null);
    requestResearchAdvice(props.runId).then(
      (response) => {
        setResult(response);
        setStatus("done");
      },
      (caught: unknown) => {
        setError(caught instanceof Error ? caught.message : "failed to get advice");
        setStatus("error");
      },
    );
  };

  const advice = result?.available ? result.advice : null;

  return (
    <Card padding="md">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          AI suggestions
        </h3>
        <Button size="sm" onClick={run} disabled={status === "loading"}>
          {status === "loading" ? "thinking…" : "suggest experiments"}
        </Button>
        <span className="text-xs text-zinc-500">
          reads this run&apos;s report and findings and proposes experiments to try — advice
          only, nothing is applied
        </span>
      </div>

      {status === "error" && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{error}</p>
      )}

      {status === "done" && result !== null && !result.available && (
        <p className="mt-2 text-sm text-zinc-500">
          the advisor is off or unavailable — set <code>TRADEBOT_AI_ADVISOR_ENABLED</code> and
          an API key on the backend to turn it on
        </p>
      )}

      {advice !== null && (
        <div className="mt-3 space-y-3">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">{advice.diagnosis}</p>
          {advice.hypotheses.length > 0 && (
            <ul className="space-y-2">
              {advice.hypotheses.map((hypothesis) => (
                <li
                  key={hypothesis.title}
                  className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3"
                >
                  <div className="flex flex-wrap items-baseline gap-2">
                    <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                      {hypothesis.title}
                    </span>
                    <span className="rounded bg-zinc-200/70 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
                      {hypothesis.family}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
                    {hypothesis.rationale}
                  </p>
                  <p className="mt-1 text-xs text-zinc-500">try: {hypothesis.parameter_hint}</p>
                </li>
              ))}
            </ul>
          )}
          <p className="text-[11px] text-zinc-400 dark:text-zinc-600">
            a recommendation only — run a sweep from the Tune tab to actually test one
          </p>
        </div>
      )}
    </Card>
  );
}
