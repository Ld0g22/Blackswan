# Statistical research and convexity — Taleb-informed, operational

## Objective function

Seek **positive asymmetry with bounded downside**, not the highest win rate or the prettiest historical equity curve. A strategy may lose on most attempts and still be useful if rare winners are large **and realisable**. Conversely, eye-catching chart peaks are meaningless if trades cannot capture them, exits are too small, or costs overwhelm returns.

The primary observable is **per-campaign net realised GBP P&L**, with every trade, fee and unresolved position disclosed. Secondary observables: executable multiple at exact entry stake, per-attempt loss distribution, liquidity-adjusted right-tail capture, live selection throughput, missed opportunity cost, probability and magnitude of catastrophic execution loss.

Let stake amount be S, all settled sale proceeds P, and attributable execution costs C (including fees, gas, conversions, failures and any unrefunded capital). Define net realised profit = P - S - C for closed positions. Return on stake = (P-C)/S; profit multiple and sale multiple must be labelled explicitly. Open positions are **not** part of realised proceeds; value them separately using conservative executable unwind quotes (or mark unpriceable) and keep liability accounting intact. Partially sold positions show realised proceeds and remaining exposure separately; do not treat returned principal as profit.

## Taleb-inspired principles applied correctly

**Fat tails:** observed returns can be highly skewed; a few rare outcomes may dominate the entire sample. Treat empirical averages, variance and extrapolated tail odds as unstable until supported by enough independent observations. Do not assume normal/lognormal/power-law simply because a fit is convenient.

**Exposure matters more than prediction:** when precise tail probabilities are unknowable, focus on the shape of the payoff. Bounded per-position loss, secure execution and an exit mechanism that preserves upside often matter more than forecasting a 100× with false precision.

**Convexity must survive execution:** a token that briefly prints 50× is irrelevant if a real stake cannot be sold at scale. Ask whether a small additional move in underlying opportunity creates disproportionately favourable *collectible* P&L without increasing unbounded downside.

**Fragility and ruin:** positive right-tail speculation does not justify hidden left-tail exposure. Model wallet compromise, unbounded approvals, repeated retry buys, illiquid exits, platform/RPC failures, fee spikes and operational downtime. A nominal 1p purchase can still create disproportionate loss if authorization is unsafe.

**The fourth quadrant:** rare events with consequential nonlinear payoffs make precise estimates fragile; use scenario and exposure bounds, robust comparisons, and conservative reporting instead of overstated confidence or fabricated probabilities.

These are working decision principles, not the claim that Taleb has proved this particular strategy profitable.

## Experimental design

1. **Define the population:** all eligible launches detected on the selected chain and time window; record exclusions, missing data, chain gaps and pool/protocol coverage. Distinguish *detected*, *selected*, *quoteable*, *sacrificed*, *main-bought*, *sold*, *failed* and *unknown*.
2. **Choose time of information:** all selector features must be available by the decision timestamp. A 30-second selector cannot use a 60-second result, subsequent peak or survival outcome. No look-ahead or refitting based on future returns.
3. **Lock the policy before evaluation:** write the exact selector rules, version, thresholds, sample windows, exit schedule and success criterion. Record how many ideas were tried; avoid quietly selecting the best of many backtests.
4. **Evaluate policy on comparable cohorts:** buying-block vs concentration with the *same detector, base data, stake size, execution model and exit logic*, unless testing that dimension on purpose. Control for time-of-day, launch clusters and structural protocol changes. Prefer time-ordered out-of-sample testing and later live small-money validation.
5. **Measure tail capture, not just hit rate:** report count/rate crossing 2×, 3×, 5×, 10×, 25×, 50× or other explicitly requested thresholds **at executable sell size**, amount actually realised under exit schedule, drawdowns, ruin/failure scenarios, and top-contributor concentration (largest 1/5/10 trades share of aggregate profit). Thresholds are analysis bins, not automatic exit changes.
6. **Include failures and censored outcomes:** missing sell quote is not zero or infinity by default; distinguish unavailable, rejected, dead, stale, unobserved and censored. Mark detection/quote coverage; do not silently drop difficult tokens.
7. **Account for selection-induced impact:** live purchases might change visibility, price, liquidity, or induce responsive selling. Compare paper and live outcomes cautiously. Record actual signed input/output, size and block, pool reserves at execution where knowable. Never assume observed early winners are independent events.
8. **Stress sensitivity:** timing delay (+1/+3/+5/+10 seconds), worst plausible slippage, limited liquidity, network throttling, failed broadcasts, fee regimes, different entry sizes, quote gaps and changes in token universe. Show whether profit remains dependent on a single exceptional trade.

## Analytical reporting template

For each evaluated policy and cohort, provide:

- Exact policy version, detector version, start/end, number of opportunities and observed coverage.
- Counts: detected / eligible / selected / sacrificed / bought / exit completed / unknown / failure.
- Exposure: stake sum, total cost, net settled sale proceeds, remaining inventory, net realised GBP profit, largest possible execution loss supported by architecture.
- Tail distribution: empirical multiple bins at **realisable stake size**, largest wins with full on-chain evidence if live, top-tail contribution, and rate of no viable sell.
- Operational quality: p50/p95/p99 detection-to-buy, quote-to-fill drift, missed deadline rate, outage/gap incidence and oldest untracked open position.
- Comparison: same-cohort selector A/B differences, uncertainty, whether observed differences could be noise, and explicit reasons a result could be biased.
- Decision: retain / reject / investigate, with specific falsifying next test and **no fabricated confidence intervals**.

## Recommended research practice (not auto-authorised strategy changes)

- Test **loss severity first**, then right-tail capture; more extreme winners cannot rescue a system that can lose beyond the intended stake.
- Compare low-cost filters without overfitting and preserve raw launch observations and timestamps. Prefer policy simplicity unless extra features demonstrate out-of-sample incremental value.
- Report both **win rate** and *payoff magnitude*; expect a low win rate may be compatible with convex returns, but never claim positive expectancy from win rate alone.
- For fat-tail samples, show raw outcomes, medians, quantiles with sample sizes, leave-top-one-out and leave-top-five-out results, and scenario envelopes. If confidence bounds are used, explain assumptions; naive Gaussian standard errors are not credible for unvalidated tails.
- Never optimise to a synthetic maximum 'possible sell multiple' when the actual exit ladder, liquidity, monitoring cadence and taxes determine collectable proceeds.
- Record all comparisons even when unprofitable. Experiments should update beliefs, not manufacture a winning backtest.

## Primary conceptual references

- Nassim N. Taleb, *Errors, robustness, and the fourth quadrant* (2009): https://www.sciencedirect.com/science/article/pii/S016920700900096X
- Nassim N. Taleb, *Statistical Consequences of Fat Tails* (arXiv): https://arxiv.org/abs/2001.10488
- Taleb, *The Fourth Quadrant: A Map of the Limits of Statistics*: https://www.edge.org/conversation/nassim_nicholas_taleb-the-fourth-quadrant-a-map-of-the-limits-of-statistics
- Taleb, *On the Statistical Differences between Binary Forecasts and Real-World Payoffs*: https://arxiv.org/abs/1907.11162

Read these as cautions about inference and exposure, not an external authority for specific token-selection thresholds.
