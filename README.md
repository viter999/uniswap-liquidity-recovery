# Uniswap in-range liquidity recovery after large price moves — preregistered pilot

**Status: complete. Preregistration frozen (v0.3, 17 October 2026); pipeline code frozen (17 October 2026) after passing the
2023 calibration checks; the one look ran once on 20 October 2026. Result: H1 No, H2-Arbitrum No, H2-Base Null. The report
is [`REPORT.md`](REPORT.md).**

## The question

After a large, fast move in the ETH/USDC price, how quickly does the liquidity that actually sits near the current price
(in-range depth) return to its pre-shock level in Uniswap v3 pools, and does that recovery speed differ

- **H1** by fee tier: the 0.05% pool versus the 0.3% pool on the same chain, and
- **H2** by chain: Arbitrum and Base versus Ethereum mainnet, same tier?

Depth near the price is what a trader actually gets. Prior work describes Uniswap v3 liquidity surfaces and the behaviour of
liquidity providers during the May 2022 stablecoin de-pegs, but nobody has defined a shock event on Uniswap pools and measured
the time for in-range depth to come back, across tiers and chains. This pilot does that once, with the rules fixed before any
depth data is read.

## Result

| Test | n | p | Holm p | Median ratio | 90% CI | Verdict |
|---|---|---|---|---|---|---|
| H1 (fee tier) | 33 | 0.304 | 0.304 | 0.679 | [0.533, 0.825] | **No** |
| H2-Arbitrum (chain) | 25 | 0.381 | 0.762 | 0.947 | [0.388, 1.414] | **No** |
| H2-Base (chain) | 23 | 0.560 | 0.762 | 0.661 | [0.253, 1.906] | **Null** |

"No" means the 90% CI of the median ratio lies entirely below 1.5, so a 1.5× faster recovery in the hypothesised direction is
ruled out; it does not mean "no difference". A median ratio below 1 means the comparison pool (0.3% for H1; Ethereum for H2)
had the shorter recovery time on the median episode; that direction was not pre-registered and is not a finding. Per-pool
results, figures, sensitivities, every deviation from the plan and the limitations are in [`REPORT.md`](REPORT.md).

## What is in this repository

| File | What it is |
|---|---|
| `PREREG_uniswap_recovery_v0.3-public.md` | The frozen preregistration, published as a copy in which four lines carry `[REDACTED-PATH]` / `[REDACTED-PROJECT]` markers in place of a local folder path and the name of an unrelated project. Rules, numbers, dates and thresholds are those of the frozen v0.3. |
| `REDACTIONS.md` | The line numbers and categories of the four redactions, the sha256 of the private frozen v0.3 and of the public copy, and the statement that only those lines differ. |
| `outages/outage_list_20261017.md` | The sequencer / block-production incident list (S4) used by the outage rule, compiled from public status pages and post-mortems before the freeze. |
| `FREEZE_MANIFEST.md` | The sha256 values recorded at the preregistration freeze for the preregistration and its supporting files. |
| `pipeline/` | The frozen pipeline: episode detection, the price-only threshold decision, tick-liquidity reconstruction (snapshot route with band re-reads, full-replay cross-check), in-range depth, recovery metrics, the statistics, the production pull drivers and the one-look analysis runner. `pipeline/SPEC_NOTES.md` records every ambiguity met while coding against the frozen preregistration and the reading the code uses. |
| `tests/` | The synthetic test suite (no network, no chain data) and its recorded results. |
| `requirements.lock` | The exact interpreter and package versions the frozen code runs on. |
| `CODE_FREEZE_MANIFEST.md` | The sha256 of every file under `pipeline/` and `tests/` plus `requirements.lock`, as recorded at the code freeze (bytes as stored and LF-normalised). |
| `tools/freeze_hash.py` | Recomputes either manifest; `python -I tools/freeze_hash.py --verify CODE_FREEZE_MANIFEST.md` checks the published code against the freeze. |
| `REPORT.md` | The report (version 1.0): design, data and checks, results, sensitivities, interpretation, deviation log and provenance. |
| `figures/` | Figure 1 (depth-recovery curves), Figure 2 (per-episode log-ratios) and the data behind both. |
| `DEVIATIONS_public.md` | Every deviation from the plan (D1–D23), with the procedures table and the run-log corrections. |
| `tools/` (other files) | The operational wrappers the confirmatory run used around the frozen code (`run_depth.py`, `prefetch_windows.py`, `supervise.py`, `loadrule.py`, `run_analysis_guarded.py`, `coverage_check.py`; optional `smoke_2023.py`, `verify_blocktimestamp.py`) and the report tools (`report_descriptives.py`, `report_figures.py`). They are outside the code freeze; the wrappers that changed a procedure are described in `DEVIATIONS_public.md`, and every file's sha256 is in `HASHES.txt`. |
| `data/confirmatory/` | The route and creation-block override files, the price-only threshold decision (`price_only/decision.json`), the per-pool-episode results (`depth/results_<chain>.jsonl`) and the stage B check records (`depth/checks_<chain>.json`) the analysis runner requires. |
| `HASHES.txt` | SHA-256 of every file added with the result release. |

The run log (operational deviations, endpoint facts, calibration results) stays private. Every deviation from the plan is in
`DEVIATIONS_public.md`; `pipeline/SPEC_NOTES.md` records the readings fixed at the code freeze. The raw logs, header, lifetime
and series caches (gigabytes) are not published; a dataset release is still to be decided.

## What the code does, in order

1. **Price-only phase** (`price_only.py`, preregistration §5.6): clock-pool swap logs over the window, episodes for both parameter
   sets, two `slot0` reads per pool-episode, the counts N and N_conf, and the threshold decision — written before any depth data.
2. **Depth pull** (`depth_pull.py`, §5.3, §6.1–6.6, §8.4): per chain and pool-episode, the window's Mint/Burn/Swap logs with
   provenance, the tick-band snapshot at the window start, replay with band re-reads, the end-of-minute depth series for Z = 0.5%,
   1%, 2%, the recovery metrics, the validity flags, and the stage B checks.
3. **One look** (`run_analysis.py`, §7): refuses to run unless every chain's checks passed; then the three confirmatory tests with
   verdicts, the pre-declared sensitivities without verdicts, resilience scores and pair tallies.

The calibration driver (`stage_a.py`) ran the §6.1 stage A checks on 2023 Ethereum data: the reconstructed active liquidity
equalled the `liquidity` field of every one of the 2,039,803 swaps of 2023 in both Ethereum pools, and the snapshot and
full-replay depth series agreed exactly on all five calibration windows.

The two pull drivers refuse to run without an explicit `--go` flag; the confirmatory window was pulled once, after the
operator's go-ahead. The per-pool-episode results and stage B check records are published under `data/confirmatory/depth/`;
the pull records are not part of this release.

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
- The reconstruction of tick liquidity is checked against the `liquidity` field of every swap on the full-replay chains
  (Ethereum, Arbitrum) and on in-band swaps of the snapshot route elsewhere, with checked and unchecked counts reported per
  pool-episode; on drawn episodes the snapshot and full-replay series are compared; a failed check stops the analysis and is logged.

## Running the tests

```
python -m venv .venv && .venv/Scripts/pip install -r requirements.lock
python -m pytest tests -q
```

## Reproducing the one look

Full detail is in the report's section 7 and Appendix G.

1. **Check the frozen code:** `python -I tools/freeze_hash.py --verify CODE_FREEZE_MANIFEST.md` should report
   "verified 35 files, 0 problems".
2. **Re-run the analysis from the published results:** with the files under `data/confirmatory/` in place, run
   `tools/run_analysis_guarded.py` with its defaults. The runner refuses unless every chain's stage B checks passed; with the
   same inputs and the pinned scipy it is deterministic (bootstrap seed 20261017).
3. **Re-pull from the chains,** in the frozen order: (1) `python -m pipeline.price_only --go`; (2)
   `tools/run_depth.py --chain <chain> --go` per chain, Arbitrum via the pre-fetch wrapper and then the driver with the hb10
   route file and the driver creation override (report Appendix G.5); (3) `tools/coverage_check.py --chain ethereum,arbitrum`;
   (4) `tools/run_analysis_guarded.py` with defaults. A fresh pull depends on the public endpoints still serving the same data.

## Licence and citation

Preregistration and documents: CC-BY-4.0. Code (`pipeline/`, `tests/`, `tools/`): MIT (`LICENSE-CODE`). Data
(`data/confirmatory/`): CC-BY-4.0 (`LICENSE-DATA`). Please cite the
frozen preregistration by the sha256 of the private v0.3 given in `REDACTIONS.md` when referring to the design; the public
copy's own sha256 is listed beside it, and the code by the hashes in `CODE_FREEZE_MANIFEST.md`.
