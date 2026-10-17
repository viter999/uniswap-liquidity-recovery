# Uniswap in-range liquidity recovery after large price moves — preregistered pilot

**Status: preregistration frozen (v0.3, 17 October 2026). No data has been pulled and no result exists yet.**

## The question

After a large, fast move in the ETH/USDC price, how quickly does the liquidity that actually sits near the current price
(in-range depth) return to its pre-shock level in Uniswap v3 pools, and does that recovery speed differ

- **H1** by fee tier: the 0.05% pool versus the 0.3% pool on the same chain, and
- **H2** by chain: Arbitrum and Base versus Ethereum mainnet, same tier?

Depth near the price is what a trader actually gets. Prior work describes Uniswap v3 liquidity surfaces and the behaviour of
liquidity providers during the May 2022 stablecoin de-pegs, but nobody has defined a shock event on Uniswap pools and measured
the time for in-range depth to come back, across tiers and chains. This pilot does that once, with the rules fixed before any
depth data is read.

## What is in this repository

| File | What it is |
|---|---|
| `PREREG_uniswap_recovery_v0.3-public.md` | The frozen preregistration, published as a copy in which four lines carry `[REDACTED-PATH]` / `[REDACTED-PROJECT]` markers in place of a local folder path and the name of an unrelated project. Rules, numbers, dates and thresholds are those of the frozen v0.3. |
| `REDACTIONS.md` | The line numbers and categories of the four redactions, the sha256 of the private frozen v0.3 and of the public copy, and the statement that only those lines differ. |
| `outages/outage_list_20261017.md` | The sequencer / block-production incident list (S4) used by the outage rule, compiled from public status pages and post-mortems before the freeze. |
| `FREEZE_MANIFEST.md` | The sha256 values recorded at the freeze for the preregistration and its supporting files. |

No code, no data and no run log are published yet. Code is published at the code freeze; the dataset and the report are published
with the one-look result.

## The design in one paragraph

Shocks are detected on the Ethereum 0.05% pool's own price: a 5% move in natural-log units within 60 minutes starts an episode,
triggers less than 48 hours apart chain into one episode, and the other five pools join an episode only if their own price
moved at least 4% the same way. For every pool-episode the metric is USD market depth within ±1% of the pool price, sampled
every minute from the pool's tick-level liquidity state, and the outcome is the time after the last trigger until depth stays
at or above 90% of its pre-shock level for 15 consecutive minutes, censored at 72 hours. Each episode contributes one number per
test (the mean of its valid within-chain or within-tier log-ratios of recovery times), tested with a Wilcoxon signed-rank test
on episodes; a verdict needs both p < 0.05 and a median ratio of at least 1.5, with at least 20 episodes. Everything else is
reported as sensitivity analysis without a verdict.

## What would make us wrong

- If the pool-clock count of confirmable episodes is below 24, the threshold switches to 3% over 30 minutes by a rule written
  before any depth data is read (prereg §5.6); the switch is logged, not chosen.
- Incentive programmes, the UNIfication fee switch, long multi-day cascades and sequencer outages are flagged by pre-written
  windows and handled in pre-declared sensitivity runs (prereg §7.8, §10.2).
- The reconstruction of tick liquidity is checked against the `liquidity` field of every swap and, on drawn episodes, against
  a full-history replay; a failed check stops the analysis and is logged.

## Licence and citation

Preregistration and documents: CC-BY-4.0. Code (when published): MIT. Please cite the frozen preregistration by the sha256 of
the private v0.3 given in `REDACTIONS.md` when referring to the design; the public copy's own sha256 is listed beside it.
