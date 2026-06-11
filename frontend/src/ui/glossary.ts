/**
 * One definition per trading term, in plain words. The research and analytics
 * screens scatter jargon — R, expectancy, profit factor, MAE/MFE — and define
 * it inconsistently or not at all. Centralising the wording here lets every
 * surface explain a term the same way (via an InfoTooltip keyed on these), and
 * keeps the phrasing aligned with the backend's verdict bands. Definitions are
 * descriptive, not arithmetic — nothing here computes on money.
 */
export interface GlossaryTerm {
  term: string;
  definition: string;
}

export const GLOSSARY = {
  r: {
    term: "R",
    definition:
      "One R is the amount risked on a trade — the distance from entry to the protective stop. Results are measured in R so trades of different sizes compare fairly: +2R means the trade made twice what it risked.",
  },
  expectancy: {
    term: "Expectancy (R)",
    definition:
      "The average R won or lost per trade. Above 0 makes money over many trades; below 0 loses it. It only means something once there are enough trades to trust.",
  },
  profitFactor: {
    term: "Profit factor",
    definition:
      "Total winnings divided by total losses. Above 1.0 means wins outweighed losses; below 1.0 means the reverse. 1.0 is break-even before fees.",
  },
  winRate: {
    term: "Win rate",
    definition:
      "The share of trades that finished in profit. On its own it says little — a low win rate with large wins can still make money, and the reverse can lose it.",
  },
  mae: {
    term: "MAE",
    definition:
      "Maximum adverse excursion — the worst unrealised loss a trade sat through before it closed. Large MAE on winners means the stop was nearly hit.",
  },
  mfe: {
    term: "MFE",
    definition:
      "Maximum favourable excursion — the best unrealised profit a trade showed before it closed. Large MFE on losers means profit was given back.",
  },
  oracle: {
    term: "Oracle",
    definition:
      "The best result a perfect trader could have gotten from the same moment. It is the yardstick a decision is graded against, not something the bot can reach.",
  },
  expectancySample: {
    term: "Sample size",
    definition:
      "How many trades the stats are based on. Small samples are mostly luck; a few dozen trades at minimum before any number is worth acting on.",
  },
} satisfies Record<string, GlossaryTerm>;
