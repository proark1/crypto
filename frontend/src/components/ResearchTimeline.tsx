import type { TimelineEventResponse } from "../api/types";
import { formatTime } from "../lib/format";
import { Badge, Card, type BadgeTone } from "../ui";

/**
 * The research story, newest first: every completed evaluation run (with
 * which mistake patterns appeared or stopped firing versus the bot's
 * previous run), every sweep verdict, and every settings promotion. The
 * headlines arrive composed from the backend so this feed, the logs, and
 * Telegram all tell the same sentence; this component only adds tone and
 * linkage.
 */
export function ResearchTimeline(props: {
  events: TimelineEventResponse[];
  /** Jump to a run's report on the Evaluate tab. */
  onSelectRun: (runId: number) => void;
}) {
  return (
    <Card padding="md">
      <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">research timeline</h3>
      <p className="mt-0.5 text-xs text-zinc-500">
        what the learning loop did, newest first: runs graded, mistake patterns that appeared or
        stopped firing versus the previous run, sweep verdicts, and the settings changes they
        earned. In-flight work is on the improver card in Tune; this is the record.
      </p>
      {props.events.length === 0 ? (
        <p className="mt-3 text-sm text-zinc-500">
          nothing yet — the first completed evaluation run starts the story
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {props.events.map((event) => {
            const runId = event.run_id;
            return (
              <li
                key={[
                  event.kind,
                  event.run_id,
                  event.sweep_id,
                  event.version_id,
                  event.at,
                ].join("-")}
                className="rounded-lg border border-zinc-200 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-950/60 p-3 text-sm"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={kindTone(event)}>{event.kind}</Badge>
                  {event.verdict !== null && (
                    <Badge tone={verdictTone(event.verdict)}>
                      {event.verdict.replace(/_/g, " ")}
                    </Badge>
                  )}
                  {(event.status === "failed" || event.status === "interrupted") && (
                    <Badge tone="red">{event.status}</Badge>
                  )}
                  <span className="ml-auto text-xs text-zinc-500">{formatTime(event.at)}</span>
                </div>
                <div className="mt-1 font-semibold text-zinc-900 dark:text-zinc-100">
                  {runId !== null ? (
                    <button
                      type="button"
                      onClick={() => {
                        props.onSelectRun(runId);
                      }}
                      className="text-left hover:text-sky-600 dark:hover:text-sky-400"
                    >
                      {event.headline}
                    </button>
                  ) : (
                    event.headline
                  )}
                </div>
                {event.detail && <p className="mt-0.5 text-xs text-zinc-500">{event.detail}</p>}
                {(event.new_patterns.length > 0 || event.resolved_patterns.length > 0) && (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {event.new_patterns.map((pattern) => (
                      <span
                        key={`new-${pattern}`}
                        className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px] text-amber-700 dark:text-amber-400"
                      >
                        new: {pattern}
                      </span>
                    ))}
                    {event.resolved_patterns.map((pattern) => (
                      <span
                        key={`resolved-${pattern}`}
                        className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[11px] text-emerald-700 dark:text-emerald-400"
                      >
                        no longer firing: {pattern}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function kindTone(event: TimelineEventResponse): BadgeTone {
  if (event.kind === "promotion") {
    return "emerald";
  }
  return event.kind === "sweep" ? "violet" : "sky";
}

function verdictTone(verdict: string): BadgeTone {
  if (verdict === "validated") {
    return "emerald";
  }
  if (verdict === "overfit") {
    return "red";
  }
  return verdict === "baseline_best" ? "zinc" : "amber";
}
