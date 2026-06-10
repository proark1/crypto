/**
 * Plain-words interpretation of evaluation numbers. Display logic only:
 * values are parsed for comparisons and color tones, never for money
 * arithmetic — the strings remain the source of truth.
 *
 * The thresholds mirror the backend's frozen verdict bands
 * (ARCHITECTURE.md §12.2) and the reporting conventions of §12.3.
 */

export type Tone = "good" | "bad" | "warn" | "neutral";

export const MIN_TRADES_TO_JUDGE = 20;
/** Below this many graded trades, expectancy is noise, not signal. */

export interface RunReading {
  tone: Tone;
  headline: string;
  explanation: string;
  nextSteps: string[];
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

/** Read the whole run: is this good, bad, or too thin to call? */
export function interpretRun(summary: Record<string, unknown>): RunReading {
  const trades = asNumber(summary.trade_count) ?? 0;
  const scenarios = asNumber(summary.scenario_count) ?? 0;
  const expectancy = asNumber(summary.expectancy_r);
  const profitFactor = asNumber(summary.profit_factor);

  if (trades < MIN_TRADES_TO_JUDGE) {
    return {
      tone: "warn",
      headline: "Too few trades to judge",
      explanation:
        `Only ${String(trades)} of ${String(scenarios)} scenarios produced a trade. With a sample ` +
        "this small the numbers below are mostly luck — do not draw conclusions " +
        "from them yet.",
      nextSteps: [
        "Run again with more history (days) and more scenarios per coin.",
        "Check the decisions journal on the trade screen: if most entries are " +
          "gated or vetoed, the strategy rarely got to act — that, not the " +
          "strategy itself, is what this run measured.",
      ],
    };
  }
  const losing = (expectancy ?? 0) <= 0 || (profitFactor ?? 0) < 1;
  if (losing) {
    return {
      tone: "bad",
      headline: "This configuration lost money in the test",
      explanation:
        `On average each trade returned ${String(expectancy ?? "?")}R (R = the amount risked ` +
        `at the stop), and the profit factor was ${String(profitFactor ?? "?")} — below 1.0 ` +
        "means the losses outweighed the wins. The bot would have shrunk the " +
        "account trading like this.",
      nextSteps: [
        "Read the findings below: each names a condition where entries lost money.",
        "Accept the findings you believe (this records your judgement — it never " +
          "changes the bot by itself).",
        "Start a parameter sweep (bottom of this page) to challenge the current " +
          "configuration; only a sweep verdict of “validated” is evidence a change " +
          "actually helps.",
        "Until something validates, the safe state is what you have: paper mode.",
      ],
    };
  }
  return {
    tone: "good",
    headline: "This configuration made money in the test",
    explanation:
      `On average each trade returned +${String(expectancy)}R (R = the amount risked at the ` +
      `stop), with a profit factor of ${String(profitFactor)} — above 1.0 means wins ` +
      "outweighed losses.",
    nextSteps: [
      "Check the breakdowns: an edge that exists only in one condition (e.g. " +
        "only in uptrends) is fragile.",
      "Run a parameter sweep to see whether a variant beats this configuration " +
        "on untouched data.",
      "Let it keep paper trading; consistent paper results over weeks matter " +
        "more than one good test.",
    ],
  };
}

/** Tone for the expectancy headline metric. */
export function expectancyTone(value: unknown): Tone {
  const parsed = asNumber(value);
  if (parsed === null) {
    return "neutral";
  }
  return parsed > 0 ? "good" : parsed < 0 ? "bad" : "neutral";
}

/** Tone for the profit-factor headline metric (1.0 is break-even). */
export function profitFactorTone(value: unknown): Tone {
  const parsed = asNumber(value);
  if (parsed === null) {
    return "neutral";
  }
  return parsed > 1 ? "good" : parsed < 1 ? "bad" : "neutral";
}

export const TONE_TEXT_CLASS: Record<Tone, string> = {
  good: "text-emerald-400",
  bad: "text-red-400",
  warn: "text-amber-400",
  neutral: "text-zinc-100",
};

export const TONE_PANEL_CLASS: Record<Tone, string> = {
  good: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
  bad: "border-red-500/40 bg-red-500/10 text-red-200",
  warn: "border-amber-500/40 bg-amber-500/10 text-amber-200",
  neutral: "border-zinc-700 bg-zinc-800/60 text-zinc-200",
};

/** What each scenario verdict means, in the §12.2 bands, with its tone. */
export const VERDICT_LEGEND: Record<string, { tone: Tone; meaning: string }> = {
  excellent: { tone: "good", meaning: "trade earned at least +1.5R" },
  good: { tone: "good", meaning: "trade earned +0.25R to +1.5R" },
  neutral: { tone: "neutral", meaning: "between -0.25R and +0.25R — fees and noise" },
  bad: { tone: "bad", meaning: "trade lost -0.25R to -1R" },
  very_bad: { tone: "bad", meaning: "trade lost -1R or worse (rode into the stop)" },
  correct_hold: { tone: "good", meaning: "rightly stayed out (or stayed in)" },
  wrong_hold: { tone: "bad", meaning: "held while the stop got hit" },
  missed_opportunity: {
    tone: "warn",
    meaning: "stayed flat while at least +1R was available",
  },
};

export const VERDICT_CHIP_CLASS: Record<Tone, string> = {
  good: "bg-emerald-900/50 text-emerald-300",
  bad: "bg-red-900/50 text-red-300",
  warn: "bg-amber-900/50 text-amber-300",
  neutral: "bg-zinc-800 text-zinc-300",
};
