import type { CandidacyConditionResponse, RoutingCandidacyResponse } from "../api/types";
import { Card } from "../ui";
import { strategyLabel } from "./ComparisonPanel";

/**
 * The §13.7 routing-candidacy gate, surfaced read-only. For each research
 * family it shows the three evidence conditions — a validated edge in a named
 * regime, beating the incumbent across comparison batches weeks apart, and an
 * eight-week positive live-paper soak — and whether all three are met.
 *
 * It only *flags* candidacy. Routing a family into the production router (which
 * regime activates it, at whose expense) stays a human architecture decision;
 * this panel never changes what trades.
 */

const CONDITIONS: { key: keyof RoutingConditions; label: string }[] = [
  { key: "validated_edge", label: "Validated edge in a regime" },
  { key: "beats_incumbent", label: "Beats the incumbent (≥2 batches, weeks apart)" },
  { key: "live_paper", label: "8-week positive live-paper soak" },
];

type RoutingConditions = Pick<
  RoutingCandidacyResponse,
  "validated_edge" | "beats_incumbent" | "live_paper"
>;

function ConditionRow(props: { label: string; condition: CandidacyConditionResponse }) {
  const met = props.condition.met;
  return (
    <li className="flex gap-2">
      <span
        className={met ? "text-emerald-600 dark:text-emerald-400" : "text-zinc-400"}
        aria-hidden="true"
      >
        {met ? "✓" : "○"}
      </span>
      <span>
        <span className={met ? "text-zinc-700 dark:text-zinc-200" : "text-zinc-500"}>
          {props.label}
        </span>
        <span className="block text-xs text-zinc-500">{props.condition.detail}</span>
      </span>
    </li>
  );
}

function FamilyCandidacy(props: { candidacy: RoutingCandidacyResponse }) {
  const { candidacy } = props;
  return (
    <div className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold text-zinc-800 dark:text-zinc-100">
          {strategyLabel(candidacy.family)}
        </h4>
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
            candidacy.is_candidate
              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200"
              : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
          }`}
        >
          {candidacy.is_candidate ? "candidacy met" : "building evidence"}
        </span>
      </div>
      <ul className="mt-2 space-y-1.5 text-sm">
        {CONDITIONS.map((condition) => (
          <ConditionRow
            key={condition.key}
            label={condition.label}
            condition={candidacy[condition.key]}
          />
        ))}
      </ul>
    </div>
  );
}

export function RoutingCandidacyPanel(props: { candidacies: RoutingCandidacyResponse[] }) {
  return (
    <Card padding="md">
      <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
        routing candidacy (§13.7)
      </h3>
      <p className="mt-1 text-xs text-zinc-500">
        whether each research family has earned the evidence to be considered for the production
        router — flag only. Which regime activates a family, at whose expense, stays a human
        decision; meeting the gate never routes anything.
      </p>
      {props.candidacies.length === 0 ? (
        <p className="mt-3 text-xs text-zinc-500">no research families to assess yet</p>
      ) : (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {props.candidacies.map((candidacy) => (
            <FamilyCandidacy key={candidacy.family} candidacy={candidacy} />
          ))}
        </div>
      )}
    </Card>
  );
}
