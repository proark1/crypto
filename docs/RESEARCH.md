# Research: How to Build a Good Crypto Spot Trading Bot

A synthesis of practitioner wisdom (r/algotrading, YouTube quant channels, vendor
engineering blogs) and authoritative academic / tooling sources on what separates a
spot trading bot that survives live markets from one that only looks good in a
backtest. This document exists to inform design decisions in
[`ARCHITECTURE.md`](../ARCHITECTURE.md); where a finding has a direct bearing on our
code it is called out under **Implication**.

## How this was produced

A fan-out research pass ran ~88 search/fetch/verify agents. Every factual claim below
was put through **3-vote adversarial verification** (a claim survives only if it is not
refuted by a 2/3 majority of skeptics instructed to break it). Of 60 verdict votes, 53
upheld the surviving claims; the 7 refutations correctly killed three weak claims, which
are retained below under **Refuted / use with care** so the same mistakes are not
repeated.

Confidence is labelled per finding:

- **[Verified]** — survived adversarial verification against a primary or strong source.
- **[Consensus]** — near-universal community wisdom (forums/YouTube) that the harness did
  not tie to a single citable primary source. Directionally reliable, not formally proven.
- **[Refuted]** — a popular claim that did **not** survive; recorded so we avoid it.

> **Source transparency.** The harness surfaced fetchable academic and tooling sources
> (linked in the bibliography). It did **not** capture stable Reddit/YouTube permalinks —
> those platforms were rate-limited or summarised without URLs during the run — so
> community-sourced points are paraphrased as **[Consensus]** rather than individually
> linked.

---

## 1. The backtest-to-live gap is the central problem

**[Verified] Backtest edge typically degrades or disappears in live trading.** Live
execution incurs deeper order-book slippage, venue outages, and shifting liquidity that
the backtest never modelled. This is the loudest, most corroborated signal across both
quant literature and practitioner reports.

**[Verified] Three real mechanisms drive the gap:**

1. **Selection noise from over-testing** — trying many strategy variants and keeping the
   winner.
2. **Fill optimism** — backtests fill at the mid-quote while live trading crosses the
   spread.
3. **Regime change** — the live market enters a state absent from the backtest data.

> ⚠️ These are *real* causes, but no source establishes them as *the* definitive top
> three. **Survivorship bias** (delisted coins vanish from history and flatter every
> backtest) and **look-ahead bias / data leakage** are cited just as often. Treat any
> ranked "top causes" list skeptically.

**[Verified] Cautionary tale — Long-Term Capital Management.** LTCM lost ~44% in a single
month (August 1998) after Russia's default, partly because its risk model was calibrated
on a lookback window that *excluded* the 1987 crash and the 1994 bond crisis. It is the
canonical illustration of regime/lookback bias: **a risk model is only as good as the
worst event in its training window.** (The full collapse was also driven by ~100:1
leverage and correlation spikes — lookback bias was a contributing illustration, not the
sole cause.)

> **Implication.** Our `backtest/` fill simulator must model spread-crossing and slippage
> pessimistically — fill optimism is the single largest source of phantom backtest
> profit. The `signals/` regime gates earn their place precisely because the dominant
> live failure mode is regime-driven. Sourcing data that includes delisted/failed assets
> guards against survivorship bias.

---

## 2. Validate against overfitting with the right statistics

This was the richest and most rigorously verified area of the research.

**[Verified] Multiple testing inflates apparent edge — the False Strategy Theorem.**
(Bailey & López de Prado.) With just 100 zero-skill trials you can *expect* a maximum
Sharpe of ~2.5 purely by chance. So a backtested Sharpe of 2.0 found after tuning many
variants can be pure selection noise.

**[Verified] Use the Deflated Sharpe Ratio (DSR).** It corrects an observed Sharpe for
the two leading inflation sources:

- **Selection bias under multiple testing** — penalises by the *effective* number of
  trials (correlated trials count as fewer independent ones).
- **Non-normal returns** — uses skewness and kurtosis to widen the standard error of the
  Sharpe estimate, via the `√((1 − γ₃·SR + ((γ₄−1)/4)·SR²) / (n−1))` term inherited from
  the Probabilistic Sharpe Ratio (the skew/kurtosis factor is the asymptotic variance;
  dividing by the sample size `n−1` turns it into the standard error of the estimate).

The DSR **outputs a probability (0–1)** that the strategy has genuine skill rather than
being a lucky draw — it is *not* a rescaled Sharpe value.

**[Verified] Standard k-fold cross-validation is invalid for financial data.** It assumes
IID observations, which markets violate (non-stationarity, autocorrelation, regime
shifts). Robust evaluation must account for these properties; naive CV leaks information
between train and test sets.

**[Verified] Use Combinatorial Purged Cross-Validation (CPCV).** It reduces leakage via:

- **Purging** — drop training rows whose label horizon overlaps the test period.
- **Embargoing** — drop a fixed fraction of rows immediately after each test fold, to
  block leakage from delayed market reactions / serial correlation.

A peer-reviewed 2024 study (*Knowledge-Based Systems*, Arian, Norouzi Mobarekeh & Seco) found
CPCV beats K-Fold, Purged K-Fold, and plain Walk-Forward on overfitting metrics — lower
Probability of Backtest Overfitting (PBO) and a higher Deflated Sharpe Ratio test
statistic. Walk-Forward specifically showed weaker false-discovery prevention (higher
temporal variability, weaker stationarity).

> ⚠️ **Honest caveat.** That comparative result comes from a *synthetic* controlled
> environment, and CPCV is computationally expensive and implementation-bug-prone.
> Walk-Forward remains the industry standard for *realistic trading simulation* — it is
> only weaker at *false-discovery prevention*. **Use both:** CPCV/DSR to decide whether an
> edge is real, walk-forward to estimate live behaviour.

> **Implication.** The CI golden backtest enforces *determinism*, but determinism does not
> protect against *overfitting*. Add a DSR / PBO gate to `backtest/` walk-forward reports
> so a strategy cannot graduate to paper trading on a flattering Sharpe alone. This
> complements the existing research-promotion gates.

---

## 3. Tooling and infrastructure

**[Verified] CCXT is the de-facto execution-adapter library.** One unified API across
~100+ exchanges, implementing both public (market data) and private (order placement)
APIs, explicitly aimed at algorithmic trading, backtesting, and bot development, with
optional normalised cross-exchange data and Python 3 support.

> ⚠️ Order-placement completeness **varies per exchange** — verify the specific venue
> rather than assuming parity across all 100+.

> **Implication.** Fits our `execution/` adapter-interface design directly. Whichever
> venue we target, validate its private-API order paths against the mock exchange under
> fault injection (disconnects, partial fills) before trusting parity.

---

## 4. Practitioner consensus (community wisdom)

The research leaned academic, so these are flagged **[Consensus]** — near-universal in
r/algotrading and quant YouTube, aligned with our `CLAUDE.md` invariants, but not tied to
a single primary citation:

- **[Consensus] Paper trade for months before risking real money** — and expect paper
  results to still overstate live performance.
- **[Consensus] Risk management dwarfs signal quality.** Position sizing, hard stops, and
  a kill switch matter more than indicator tuning. (Our "money is `Decimal`, stops are
  exchange-native, paper is the default" invariants are exactly what survivors preach.)
- **[Consensus] Beware survivorship in your data** — delisted coins vanish from history
  and flatter every backtest.
- **[Consensus] Simple, robust strategies beat over-parameterised ones.** Fewer knobs =
  fewer ways to overfit.

---

## 5. Refuted / use with care

Popular claims that did **not** survive adversarial verification:

- **[Refuted] "A raw Sharpe of 2.0 corresponds to a deflated Sharpe of ~0.5."** Category
  error. The Deflated Sharpe Ratio is a **probability (0–1)**, not a rescaled Sharpe
  value. The *direction* (apparent edge can be selection noise) is correct; the specific
  numeric framing is not. Sourced to a low-authority blog whose own wording conflated the
  two metrics.
- **[Refuted] "These are THE three most common causes of the backtest-live gap."** The
  three mechanisms in §1 are real, but the *definitive ranking* is not supported —
  authoritative sources rank look-ahead and survivorship bias equally or higher. Traced to
  a single inaccessible (HTTP 403) vendor blog about *prediction markets*, not crypto.
- **[Refuted] "Median small-account traders lose 26.8%."** The −26.8% figure is real but
  comes from **prediction-market** data (Polymarket/Kalshi, accounts under $100), not
  crypto spot trading — a domain misattribution. The *directional* point (median
  small-account retail traders lose money) stands; the specific number does not apply to
  spot crypto.

---

## 6. What this means for our build (summary)

Our `ARCHITECTURE.md` design already encodes most of the verified best practices — strict
module boundaries (strategies never place orders), one code path across
backtest/paper/live, exchange-native stops, paper-by-default. The two highest-leverage
additions the research suggests:

1. **Add an overfitting gate** (DSR + Probability of Backtest Overfitting) to `backtest/`
   reports — not just the byte-identical golden test.
2. **Make the fill simulator pessimistic** about spread-crossing and slippage, since that
   is the #1 source of phantom backtest profit.

---

## Bibliography

Primary and strong secondary sources captured during the verified research pass.
Community (Reddit/YouTube) points in §4 are **[Consensus]** paraphrases without stable
permalinks, as noted above.

### Backtest overfitting, Deflated Sharpe Ratio, cross-validation

- Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias,
  Backtest Overfitting, and Non-Normality," *Journal of Portfolio Management*, 2014 —
  SSRN: <http://ssrn.com/abstract=2460551>;
  PDF: <http://boston.qwafafew.org/wp-content/uploads/sites/4/2017/01/Lopez_de_Prado_Sharpe.pdf>
- Hamid R. Arian, Daniel Norouzi Mobarekeh & Luis A. Seco, "Backtest overfitting in the
  machine learning era: A comparison of out-of-sample testing methods…,"
  *Knowledge-Based Systems*, Vol. 305, 2024 — DOI 10.1016/j.knosys.2024.112477
  (ScienceDirect S0950705124011110; SSRN 4686376)
- Wikipedia: Deflated Sharpe ratio — <https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio>
- Wikipedia: Purged cross-validation — <https://en.wikipedia.org/wiki/Purged_cross-validation>
- Wikipedia: Sharpe ratio — <https://en.wikipedia.org/wiki/Sharpe_ratio>
- QuantInsti: Cross-validation, embargo, purging, combinatorial —
  <https://blog.quantinsti.com/cross-validation-embargo-purging-combinatorial/>
- QuantInsti: Walk-forward optimization —
  <https://blog.quantinsti.com/walk-forward-optimization-introduction/>
- CFA Level II — Problems in backtesting —
  <https://analystprep.com/study-notes/cfa-level-2/problems-in-backtesting/>

### Regime / lookback risk (cautionary)

- Wikipedia: Long-Term Capital Management —
  <https://en.wikipedia.org/wiki/Long-Term_Capital_Management>
- Wikipedia: 1994 bond market crisis —
  <https://en.wikipedia.org/wiki/1994_bond_market_crisis>

### Execution tooling (CCXT)

- CCXT — <https://github.com/ccxt/ccxt> · docs: <https://docs.ccxt.com/> ·
  <https://ccxt.readthedocs.io/en/latest/manual.html>

### Backtest-to-live gap (practitioner / vendor engineering blogs)

- <https://bitsgap.com/blog/crypto-bot-backtesting-in-2026-what-it-shows-and-what-it-cannot-predict>
- <https://blog.alphainsider.com/why-trading-backtests-fail-live-and-how-to-fix-it/>
- <https://algopolis.com/why-backtested-trading-strategies-fail-in-real-markets/>
- <https://blog.pickmytrade.trade/algorithmic-trading-overfitting-backtest-failure/>
- <https://3commas.io/blog/ai-trading-bot-risk-management-guide-2025>
