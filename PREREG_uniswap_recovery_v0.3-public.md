# PREREG_uniswap_recovery_v0.3 — Uniswap in-range liquidity recovery after large price moves

| Field | Value |
|---|---|
| Version | v0.3 — **FROZEN 2026-10-17** after the targeted fresh-eyes check (RUNLOG.md "FREEZE v0.3" entry carries this file's sha256; any later change is a new version number) |
| Supersedes | v0.2, v0.1, v0 (all kept unchanged in this folder) |
| Folder | `[REDACTED-PATH]` (isolated; nothing outside it touched) |
| Status | Plan. No on-chain bulk download, no installs, no API keys, no wallet, no pipeline code yet. |
| Markers | `[VERIFY]` = fact to be checked at run time; the value written here stands unless a primary source moves it, and moving it after freeze is a logged deviation. No `[CONFIRM]` items remain. |

## 0. Changelog v0.2 → v0.3 (17 Oct 2026)

Operator rulings: **G4** accepted (header timestamps on Ethereum/Arbitrum, formula timestamps on Base/Unichain, status-page outage list compiled now and fixed as S4); **G12** accepted (S-i exploratory); all blocking, should-fix and minor items of the second review applied as worded; **the 2023 R1/R2 calibration check moves to after the prereg freeze and before any confirmatory-window data**, with fixes limited to `reconstruct.py` and `depth.py` and logged as deviations.

| Finding | Where applied |
|---|---|
| G1 price orientation | §5.1, §3 orientation column |
| G2 T_rec + 1 min | §6.3, §7.2, §7.3, S-c |
| G3 (θ, L) parameter sets | §5.2, §5.5, §5.6, S-i |
| G4 timestamps and outage list | §6.6, §6.5, §8.2, §10.2 S4 |
| G5, G6 censoring in test vs effect size; sign rule; zeros | §7.4, §7.5, §7.6 |
| G7 split checks, constrained fixes | §6.1, §7.10, §10.3 |
| G8 band semantics | §6.1 |
| G9 depth formula | §6.2 |
| G10 flag names, pool_absent | §7.1a, §9 |
| G11 price seed, Base windows | §5.6 (two `slot0` reads replace Swap-log pulls for non-clock pools in the price-only phase) |
| G12 S-i exploratory | §7.8 |
| G13 n with zeros; sign test | §7.6, S-d |
| G14 stratified formula | §7.9 |
| G15 pre_in_prior_recovery | §5.4, S-e |
| G16 direction | §7.2, §7.3 |
| G17 NFPM owner | §6.4 |
| G18 cross-reference, RUNLOG hashing, [VERIFY] line | §4, §10.3 |
| G19 | done in RUNLOG |
| G20 log units | §5.2, §5.5 |

---

## 1. Question

After a large, fast move in the ETH/USDC price, how quickly does the liquidity that sits near the current price (in-range depth) return to its pre-shock level in Uniswap v3 pools, and does that recovery speed differ (H1) by fee tier (0.05% vs 0.3%) and (H2) by chain (Ethereum vs Arbitrum vs Base)?

---

## 2. Related work

Unchanged from v0.2 §2 (citation-checked 17 Oct 2026; only sources actually opened). Gap statement: no public work defines a shock event on Uniswap pools and measures time-to-recover of in-range depth across fee tiers and chains.

---

## 3. Pools (verified on-chain 17 Oct 2026; v0.1 §11)

Each pool's `token0()`, `token1()`, `fee()`, `factory()` were read and `factory.getPool(token0, token1, fee)` returned the same address. Factories: Ethereum and Arbitrum `0x1F98431c8aD98523631AE4a59f267346ea31F984`; Base `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`; Unichain `0x1f98400000000000000000000000000000000003`. Liquidity = GeckoTerminal snapshot 17 Oct 2026 (informational).

| Chain | Tier | Spacing | Pool | token0 / token1 | Orientation (§5.1) | Liquidity | Role |
|---|---|---|---|---|---|---|---|
| Ethereum | 0.05% | 10 | 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640 | USDC / WETH | token0 = USDC → p = 1 / P_h | $84.5M | Confirmatory; episode clock |
| Ethereum | 0.3% | 60 | 0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8 | USDC / WETH | p = 1 / P_h | $54.6M | Confirmatory |
| Arbitrum | 0.05% | 10 | 0xC6962004f452bE9203591991D15f6b388e09E8D0 | WETH / USDC (native 0xaf88…5831) | token0 = WETH → p = P_h | $34.6M | Confirmatory |
| Arbitrum | 0.3% | 60 | 0xC473e2aEE3441BF9240Be85eb122aBB059A3B57c | WETH / USDC (native) | p = P_h | $10.1M | Confirmatory |
| Base | 0.05% | 10 | 0xd0b53D9277642d899DF5C87A3966A349A798F224 | WETH / USDC (0x8335…2913) | p = P_h | $9.1M | Confirmatory |
| Base | 0.3% | 60 | 0x6c561B446416E1A00E8E93E221854d6eA4171372 | WETH / USDC | p = P_h | $146.3M | Confirmatory |
| Unichain | v3 0.3% | 60 | 0x8927058918e3cff6f55efe45a58db1be1f069e49 | USDC (0x078d…7ad6) / WETH | p = 1 / P_h | $0.85M | Exploratory only, no verdict |

Decimals: USDC 6, WETH 18 on every chain. Excluded: Arbitrum USDC.e pools; Unichain v3 0.05% (≈$14K); Ethereum 0.01% and 1% tiers; all v4 pools. Confirmatory set = the six v3 pools on Ethereum, Arbitrum, Base. Base's size ordering is inverted (0.3% ≈ 16× the 0.05% pool); mitigations: own-baseline relative metric, pre-shock depth covariate, sensitivity S-g.

---

## 4. Sample windows

- **Confirmatory window W = [2024-01-01 00:00, 2026-10-01 00:00) UTC.** An episode is confirmatory iff its **t1_first** (§5.2) lies in W. Each pool-episode's data window (§5.3) is pulled in full even where it crosses the boundary of W.
- **Calibration window: 2023-01-01 to 2023-12-31, Ethereum pools only**, used only for the pipeline checks in **§6.1** (calibration stage). Never pooled into the confirmatory result; no test statistic is computed on it. It contains the March 2023 USDC de-peg; USDC is valued at 1 USD by definition (§6.2), so calibration depth figures around 10–13 March 2023 are known to be distorted and are not used for any decision. The 2023 data pull itself waits until the operator declares [REDACTED-PROJECT] done.
- Unichain pool covered from its creation (~Oct 2025); exploratory only.

---

## 5. Shock definition and episodes

### 5.1 Price series and orientation
- Raw pool price from a `Swap` event or `slot0`: P_h = (sqrtPriceX96 / 2^96)² × 10^(dec0 − dec1), the price of token0 in token1 in human units.
- **p = USD per ETH:** p = P_h if token0 = WETH, else p = 1 / P_h (orientation column in §3). All thresholds, directions and bands are stated in p.
- Per-minute price p(m): minute m = floor(block.timestamp / 60) in UTC; p(m) = p implied by the **last `Swap` event with timestamp in minute m**, ordered by (block_number, log_index); empty minutes carry the previous value forward. Equivalently, p(m) = the `slot0` price at the end of the last canonical block with timestamp < 60·(m + 1), because the price changes only at swaps. The clock series is the Ethereum 0.05% pool's p(m).
- Log moves are in natural-log units throughout (0.05 = 5 log-points).

### 5.2 Trigger parameters and episodes (gap-based chaining)
- Parameter set (θ, L): **primary (θ = 0.05, L = 60 min)**; **alternate (θ = 0.03, L = 30 min)**. Which set is primary is fixed by §5.6 before any depth data; the other set is exploratory (S-i).
- Trigger minute on the clock series: |log(p(m) / p(m − L))| ≥ θ.
- Episodes: trigger minutes sorted; a trigger starts a new episode iff it is **≥ 2,880 min (48 h) after the previous trigger minute**; otherwise it joins the current episode (gap-based chaining, as implemented in `proxy/proxy_episode_count.py`).
- Named quantities: **t1_first** = first trigger minute; **t1_last** = last trigger minute; **t0 = t1_first − L**; **span = t1_last − t1_first**.
- **Long clusters:** span > 48 h → kept, flagged `long_cluster`; sensitivity S-e excludes them.
- Direction on the clock series over [t0, t1_first]: `ETH_up` if log(p(t1_first) / p(t0)) > 0, else `ETH_down`. Both directions confirmatory.

### 5.3 Episode windows
- Pre-shock window: [t0 − 180 min, t0 − 15 min]. Dip window: [t0, t1_last + 30 min].
- Recovery clock starts at **t1_last**; censoring horizon **t1_last + 72 h** (4,320 min); depth sampled to **t1_last + 72 h + 15 min**.
- Data window per pool-episode: [t0 − 4 h, t1_last + 72 h + 15 min]; snapshot block b0 = the last canonical block with timestamp < t0 − 4 h.

### 5.4 Consecutive episodes
- If t0(e+1) < t1_last(e) + 72 h, every pool-episode of e is censored at min(4,320 min, t0(e+1) − t1_last(e)) and flagged `truncated_by_next` (the chaining gap guarantees t0(e+1) − t1_last(e) ≥ 47 h for L = 60 min, ≥ 47.5 h for L = 30 min).
- If t0(e+1) − 180 min < t1_last(e) + 72 h, every pool-episode of e+1 is flagged `pre_in_prior_recovery` (its pre-shock level may be a depressed post-shock level); sensitivity S-e excludes them.

### 5.5 Local confirmation (non-clock pools)
On the pool's own p series (§5.1): `local_shock = true` iff |log(p(t1_first) / p(t0))| ≥ **0.8·θ** (0.04 primary, 0.024 alternate) **with the same sign as the clock pool's move over the same two minutes**. Otherwise `local_shock = false`; the pool-episode is excluded from pairs and tallied. The clock pool has `local_shock = true` by definition.

### 5.6 Primary threshold decision (price-only, after freeze, before any depth data)
1. Pull **Swap logs of the Ethereum 0.05% pool only** over W plus margins (`price_series.py` reads nothing but `sqrtPriceX96`, block number, log index) and the headers of its log-bearing blocks (§6.6) to assign minutes.
2. Compute episodes for **both** parameter sets (§5.2).
3. Run the §6.6 50-block timestamp-formula verification on Base and Unichain now (episode windows are known after step 2), before any `slot0` read. Then, for every episode of both sets and every non-clock confirmatory pool, obtain p(t0) and p(t1_first) by **two archive `slot0` reads** at B(t0) and B(t1_first), where B(m) = the last canonical block with timestamp < 60·(m + 1) (block located by the verified formula on Base/Unichain, by binary search on headers on Ethereum/Arbitrum, §6.6). These two reads and the verification headers are the only non-Swap data allowed in this phase. Evaluate §5.5.
4. **N_conf(set) = number of confirmatory episodes with `local_shock = true` in all five non-clock confirmatory pools.** Report N(set) and N_conf(set) for both sets.
5. **If N_conf(primary) < 24 the primary set becomes (0.03, 30 min); otherwise it stays (0.05, 60 min).** The decision, the counts, and the sha256 of the price-series and `slot0` files are written to RUNLOG **before** any Mint/Burn log or tick snapshot is requested.
6. The CEX proxy count (Binance ETHUSDT closes: N = 47 / 101; RUNLOG 17 Oct 2026) is a recorded deviation used only for feasibility; the pool-clock counts govern.

---

## 6. Metric

### 6.1 Liquidity-state reconstruction and the two check stages
- **R2 (primary, all chains).** At b0 read `slot0`, `liquidity`, and the `tickBitmap` words covering **the slot0 price at b0 × (1 ± 0.08)** (band centre = slot0 price at b0, not p(t0)), then `ticks(tick).liquidityNet` for every initialised tick in the band, all batched through Multicall3 on the same endpoint. Replay the window's `Mint`, `Burn`, `Swap` logs from b0 onward. Initialised ticks: compressed = floor(tick / spacing); word = compressed >> 8; bit = compressed & 255.
- **Band re-read.** Whenever the end-of-block price leaves ±6% (= 8% − Z_max 2%) of the current band centre, the band is re-read as ±8% around the price at the end of that block (new centre), and the re-read (pool-episode, block, old centre, new centre) is logged in RUNLOG and counted in `band_reread_count`. If a re-read fails after 3 attempts, `band_read_failed = true` and the pool-episode is dropped and tallied.
- **R1 (cross-check only, Ethereum and Arbitrum):** full `Mint`/`Burn` replay since pool creation.
- **Checks, stage A (calibration, after the prereg freeze, before any confirmatory-window data):** on 2023 Ethereum data for both Ethereum pools: (i) the reconstructed active liquidity at the current tick equals the `liquidity` field of every 2023 `Swap` event; (ii) R1 and R2 depth series agree within 0.1% at every sampled minute on five calibration windows = the five largest 60-min |Δlog p| events of 2023 on the clock pool (deterministic, windows per §5.3). **Fixes after a failed stage-A check are limited to `reconstruct.py` and `depth.py`, logged in RUNLOG as deviations with the diff, and the stage is re-run.** Code freeze (§10.3) follows a passing stage A.
- **Checks, stage B (confirmatory, after the depth pull, before `analysis.py` runs):** (i) the `Swap`-`liquidity` equality on every confirmatory pool-episode; (ii) R1/R2 agreement within 0.1% on **5 confirmatory episodes drawn once by `random.Random(20261017).sample(sorted(confirmatory_episodes, key=t1_first), 5)`** and logged. Fixes limited to `reconstruct.py` and `depth.py`, diff + re-hash + re-run logged; **no T_rec is computed before stage B passes.**

### 6.2 In-range depth D_Z(m)
- Band in p: [p(m)(1 − Z), p(m)(1 + Z)], **Z = 0.01 primary** (0.005, 0.02 secondary). In raw √price units (√P_raw = sqrtPriceX96 / 2^96): if token0 = WETH the raw bounds are √P_raw·√(1 − Z) and √P_raw·√(1 + Z); if token0 = USDC they are √P_raw / √(1 + Z) and √P_raw / √(1 − Z).
- Per initialised tick range [i_lo, i_hi) with piecewise-constant active liquidity L (raw units), √pa = 1.0001^(i_lo / 2), √pb = 1.0001^(i_hi / 2), clipped to the band and, for the range containing the live price, split at the live √P_raw: **amount0 = L·(1/√pa − 1/√pb)**, **amount1 = L·(√pb − √pa)** (raw token units; USDC scaled by 10^−6, WETH by 10^−18).
- **D_sell** = USDC received selling ETH until p reaches p(m)(1 − Z): sum over the ranges between the live price and that bound of the USDC-side amount (amount1 if token1 = USDC, amount0 if token0 = USDC). **D_buy** = ETH received buying until p reaches p(m)(1 + Z): the WETH-side amount over the ranges on the other side, **valued at p(m)**. **D_Z(m) = D_sell + p(m)·D_buy**, in USD, **fee-free** (no swap fee deducted, so tiers are comparable); **USDC = 1 USD by definition.**
- Sampling: the state used for minute m is the state after the **last log-bearing block (Mint/Burn/Swap of that pool) with timestamp < 60·(m + 1)**; since the pool state changes only at such blocks, this equals the end-of-minute state, and same-block JIT mint/burn nets to zero. Minutes without such a block carry the previous state; the b0 snapshot seeds the series.
- Secondary unit-free variant: ∫ L over the ±Z tick band.

### 6.3 Pre-shock level, dip, recovery (per pool-episode)
- D_pre = median of D_Z over [t0 − 180 min, t0 − 15 min]. D_min = min of D_Z over [t0, t1_last + 30 min].
- `dip = true` iff D_min < 0.75·D_pre; `dip = false` pool-episodes are excluded from pairs in the primary analysis and tallied (sensitivity S-c).
- **T_rec** (minutes) = (first minute t ≥ t1_last with D_Z(s) ≥ 0.9·D_pre for every minute s in [t, t + 15 min)) − t1_last; T_rec may be 0. Censored at 4,320 min or the §5.4 value (`censored = true`, T_rec set to that value).
- **Every log-ratio in §7 uses T_rec + 1 min on both members** (so T_rec = 0 and S-c are well defined).
- Secondary: T_50 = time from t1_last to first reaching D_min + 0.5·(D_pre − D_min) with the same persistence rule.

### 6.4 Replenishment share (secondary, descriptive)
At T_rec, the share of D_Z from liquidity minted after t0. Positions are keyed by (owner, tickLower, tickUpper) with burns attributed LIFO on the key. **Limitation (stated):** positions held through the NonfungiblePositionManager all carry its address as `owner`, so the key merges unrelated LPs; the share is reported with this caveat and is never used in a confirmatory test.

### 6.5 Outage rule (uses the S4 list)
- `outage = true` for a chain-episode iff its window [t0 − 180 min, t1_last + 72 h] overlaps a **halt** entry of the S4 incident list (§10.2; `outages/outage_list_20261017.md`, sha256 in RUNLOG). An outage drops both pools on that chain for that episode; pairs on other chains are unaffected.
- Overlap with a **degraded** S4 entry sets `chain_degraded = true` (reported, never dropped).
- Ethereum and Arbitrum additionally: `header_gap = true` if any two fetched headers that are adjacent by block number differ in timestamp by > 600 s inside the window (reported, never dropped). On Base and Unichain timestamps are formulaic through a halt, so no timestamp-based detection exists; the S4 list is the rule there.
- A gap in a data source that is not a chain incident is a hard pipeline failure, not an outage.

### 6.6 Block timestamps and block location
- **Ethereum and Arbitrum:** timestamps come from `eth_getBlockByNumber(n, false)` for **every log-bearing block fetched**, sent as JSON-RPC batches of 100; a block's timestamp is never inferred. B(m) for an arbitrary minute is found by binary search over headers (≤ 30 header calls per lookup).
- **Base and Unichain (OP Stack):** timestamp(n) = genesis_timestamp + block_time × n, with genesis_timestamp **1686789347** (Base) and **1730748359** (Unichain), block_time 2 s and 1 s. Verified exactly on genesis and two recent blocks per chain on 17 Oct 2026 (`probes/probe_genesis_20261017.txt`). **Immediately after §5.6 step 2 and before any `slot0` read**, the formula is re-verified on **50 random blocks per chain inside episode windows (seed 20261017)**; any mismatch switches that chain to header fetching for log-bearing blocks (budget in §8.2) for both the price-only and the depth phases. B(m) = floor((60·(m + 1) − 1 − genesis_timestamp) / block_time).

---

## 7. Hypotheses, one look, verdict rules

### 7.1 Unit of analysis: one statistic per episode
Pairs are formed within an episode; each episode contributes **one** value to each confirmatory test; **n counts episodes**.

#### 7.1a Valid pair and tally reasons
A pair (two pool-episodes of the same episode) is **valid iff both members have `dip = true`, `local_shock = true`, `outage = false`, `band_read_failed = false`, `pool_absent = false`, and not both `censored = true`.** `pool_absent = true` iff the pool's creation block > b0 or D_pre = 0 (in practice only the Unichain pool). Invalid pairs are tallied by the first failing reason in this order: `pool_absent`, `outage`, `band_read_failed`, `local_shock`, `dip`, `both_censored`.

### 7.2 H1 — fee tier
- Claim and **hypothesised direction: d1_e > 0 and M1 > 1** (the 0.05% pool recovers faster than the 0.3% pool on the same chain).
- Pair statistic per chain c: r1_c = log((T_rec(0.3%, c) + 1) / (T_rec(0.05%, c) + 1)).
- Episode statistics (§7.4): d1_e^val and d1_e^rank from the valid chains c ∈ {Ethereum, Arbitrum, Base}.
- Test: Wilcoxon signed-rank on {d1_e^rank}, two-sided α = 0.05 (§7.5). Effect size M1 = exp(median d1_e^val); **MEI = 1.5**.
- Per-chain r1_c distributions reported descriptively, no verdict.

### 7.3 H2 — chain (one test per L2)
- Claim and **hypothesised direction: d2_e > 0 and M2 > 1** (the L2 pool recovers faster than the Ethereum pool of the same tier).
- Pair statistic per tier τ: r2_τ = log((T_rec(Ethereum, τ) + 1) / (T_rec(L2, τ) + 1)).
- Episode statistics d2_e^val, d2_e^rank over valid tiers τ ∈ {0.05%, 0.3%}.
- Tests: Wilcoxon signed-rank on {d2_e^rank(Arbitrum)} and {d2_e^rank(Base)}, two-sided α = 0.05, **Holm across these two**. M2(L2) = exp(median d2_e^val); MEI = 1.5. Per-tier results descriptive.

### 7.4 Two episode statistics: value-based and rank-based
- A valid pair is **uncensored** if neither member is censored, **one-censored** if exactly one is.
- **d_e^val** = mean of r over **all** valid pairs of the episode, with censored members at their censored value (+1). Used for M, the bootstrap CI and the sign test.
- **d_e^rank**, used only in the signed-rank test: if the episode has ≥ 1 uncensored valid pair, d_e^rank = mean of r over its **uncensored** valid pairs (episode counted as uncensored); otherwise (every valid pair one-censored) d_e^rank = sign(d_e^val) × (max |d^rank| over uncensored episodes + 1), i.e. rank-only information. The max is **fixed from the full sample** (not recomputed in resamples). If no uncensored episode exists, the test is reported as **underpowered** (no verdict).
- d_e^rank = 0 (or sign(d_e^val) = 0 in the rank-only case) is dropped from the test and counted. Counts reported: episodes with ≥ 1 valid pair, uncensored episodes, one-censored episodes, zeros dropped.

### 7.5 Implementation
- Signed-rank test: `scipy.stats.wilcoxon(d, zero_method="wilcox", correction=False, alternative="two-sided", method="auto")` (zeros dropped, average ranks, exact p for small n without ties, else normal approximation without continuity correction); scipy version pinned in `requirements.lock`.
- Bootstrap (for M's CI): **B = 10,000** percentile resamples of **episodes** (each resample recomputes d_e^val from the episode's own pairs), `numpy.random.default_rng(20261017)`, CI = 5th and 95th percentiles of exp(median d_e^val).

### 7.6 Verdict rules (per confirmatory test)
- **n** = episodes with ≥ 1 valid pair and d_e^rank ≠ 0; **underpowered** (no verdict, descriptive only) if n < 20. Per-chain and per-tier pair counts reported; no per-stratum minimum.
- **Yes:** p < 0.05 (Holm-adjusted for H2) **and** M ≥ 1.5 (direction d_e > 0).
- **No** (evaluated only if "Yes" fails): the 90% bootstrap CI of M lies entirely below 1.5 (includes effects in the opposite direction).
- **Null / inconclusive:** otherwise.
- Power note: 20 episodes give ≈ 80% power (two-sided α = 0.05, paired Wilcoxon, ARE ≈ 0.95) for a true median log-ratio of log 1.5 ≈ 0.405 only if the SD of d_e is ≈ 0.6; ≈ 50% at SD 0.9. The floor is a minimum, not a power guarantee; the observed SD is reported.
- The directional requirement makes each two-sided test effectively one-sided at α = 0.025; stated in the report.

### 7.7 Families and multiplicity
H1 and H2 are separate families; exactly **three confirmatory p-values** (H1, H2-Arbitrum, H2-Base; the latter two Holm-corrected together). Everything in §7.8 is reported without p-values and without verdicts.

### 7.8 Pre-declared sensitivity and exploratory analyses (no verdicts)
- S-a: exclude episodes flagged `incentive_v4_pull` or `structural_fee_switch`.
- S-b: on Arbitrum additionally exclude `incentive_possible`.
- S-c: `dip = false` pool-episodes enter with T_rec = 0 and their pairs are retained (ratios use T_rec + 1).
- S-d: sign test on sign(d_e^val), zeros dropped and counted.
- S-e: exclude `long_cluster` and `pre_in_prior_recovery` episodes.
- S-f: H1 with the Ethereum chain removed from d1_e (clock-pool advantage).
- S-g: H1 with Base removed from d1_e (size inversion).
- S-h: Z = 0.005 and Z = 0.02.
- S-i (**exploratory**, ruled G12): the non-primary (θ, L) set; its episodes are **not** part of the single depth pull and any depth computed for them later is exploratory by §7.10.
- S-j: the pair-level stratified statistic of §7.9.
- Descriptive: per-chain H1 and per-tier H2; up vs down; pre-shock depth covariate; `dip = false` share; replenishment share; T_50; tier contrast pre/post fee switch; `chain_degraded` and `header_gap` episodes; Unichain v3 0.3%; v4 pools.

### 7.9 Secondary pair-level stratified statistic (not confirmatory)
T = Σ_s w_s (W_s − n_s(n_s + 1)/4), w_s = 1/(n_s + 1), where s indexes strata (chains for H1, tiers for H2), W_s = sum of positive signed ranks of the n_s non-zero valid pairs in s with average ranks; Var₀(T) = Σ_s w_s² [n_s(n_s + 1)(2n_s + 1)/24 − Σ_ties (t³ − t)/48]; p from the normal approximation without continuity correction. Implemented in `resilience_score.py`, unit-tested against `scipy.stats.wilcoxon` on single-stratum inputs.

### 7.10 One look (order fixed; see §10.3 for the two freezes)
1. **Prereg freeze** (this document, S4 list, pool table; hashes in RUNLOG).
2. Pipeline written. **Stage A calibration checks on 2023 Ethereum data** (§6.1), after [REDACTED-PROJECT] is done and **before any confirmatory-window data is pulled**; fixes limited to `reconstruct.py` / `depth.py`, logged as deviations.
3. **Code freeze**: pipeline files and `requirements.lock` hashed.
4. Price-only pull and the §5.6 threshold decision, logged.
5. Depth pull for the primary set's episodes; **stage B checks**; one run of `analysis.py`; one results table. Everything after is exploratory. **No parameter other than the §5.6 threshold may change after the prereg freeze without a new version number; code changes after the code freeze are deviations logged with diffs.**

---

## 8. Data route (public RPC → Blockscout → BigQuery sandbox; no Dune, no The Graph)

### 8.1 Probe results (17 Oct 2026; v0.1 §8.1 and §11)
Ethereum: `rpc.mevblocker.io` archive logs in 10,000-block windows; `eth.drpc.org` free plan archive `eth_call` and ~100-block log windows. Arbitrum: `arb1.arbitrum.io/rpc` archive logs in 50,000-block windows. Base: `mainnet.base.org` rejects `eth_getLogs`; `base-rpc.publicnode.com` recent-only; `base.drpc.org` free plan archive `eth_call` and ~100-block log windows; `base.blockscout.com` behind a bot challenge. Unichain: `mainnet.unichain.org` archive logs in 10,000-block windows. BigQuery: a 33-month Ethereum logs scan costs 1.51 TB; **ruled out for every bulk pull** (operator note 17 Oct 2026).

### 8.2 Per-chain plan

| Need | Ethereum | Arbitrum | Base | Unichain |
|---|---|---|---|---|
| Price-only phase (§5.6): clock-pool Swap logs over W + margins | mevblocker, 10k windows, ~730 calls; 2–4M Swap logs; **headers for every log-bearing block, est. 1–2M blocks → 10–20k batched header requests** | — | — | — |
| Price-only phase: two `slot0` reads per non-clock pool-episode (both parameter sets, ≈ 150 episodes × 5 pools × 2) | ~300 reads + binary-search headers (≤ 30 per lookup) | ~300 reads + headers | ~300 reads (block by formula) | ~150 reads (block by formula) |
| Tick snapshot at b0 and any re-reads (archive `eth_call`, Multicall3) | drpc or mevblocker | official RPC | drpc | official RPC |
| Window logs (Mint/Burn/Swap), window ≈ 80 h + span | ≈ 24k blocks → 3 calls per pool-episode | ≈ 1.2M blocks → ~24 calls | **drpc 100-block windows: ≈ 144k blocks → ~1,440 calls per pool-episode; × 2 pools × ~45 episodes ≈ 130k tiny calls, throttled (≈ 4 h at 10 req/s)**; fallback order if drpc degrades: Blockscout Base REST, then BigQuery only after a console dry run of one pool-window query and operator approval | ≈ 290k blocks → 29 calls |
| Headers for log-bearing blocks in windows | est. ≤ 20k blocks per episode (both pools share blocks) → ≤ 200 batched requests per episode | est. 20–60k per episode → 200–600 batched requests | formula; **50-block verification per chain**; fallback header fetch ≈ 50k blocks per episode → 500 batched requests | formula; same verification |
| Lifetime Mint/Burn (R1 cross-check and stage A) | mevblocker ~1,700 calls | official ~600 calls | not needed | not needed |

Sizing assumes ~45 primary-set episodes; the depth pull covers the primary set only (G12).

### 8.3 Size and compute (order of magnitude)
Raw logs < 10 GB (Base 0.3% window swaps dominate), headers < 1 GB, processed < 1 GB, derived depth tables < 100 MB. Pure Python + numpy + pandas + pyarrow + scipy on one laptop; tick replay O(events); depth ≈ 200 ticks × ~1M minutes ≈ 10^8 simple operations. Data pull is rate-limit bound (hours); Base is the long pole.

### 8.4 Log hygiene and provenance
- Deduplicate logs on (blockHash, logIndex); discard `removed: true`; overlapping windows allowed.
- Per chain the pull records the **cutoff** (latest block number and hash at pull time); only blocks ≤ cutoff are used; a reorg across the cutoff invalidates the pull.
- Completeness check on **50 random blocks per chain inside episode windows (seed 20261017)**: Swap counts from the primary source compared with a second source where one exists (Ethereum: mevblocker vs drpc; Base: drpc vs publicnode for recent blocks only; Arbitrum and Unichain: official vs publicnode). Mismatch = hard failure.
- RUNLOG records per chain: endpoint, pull start/end (UTC), cutoff block and hash, call count, log count before and after dedupe, header count, formula-verification result.

---

## 9. Deliverables

1. **Open dataset** (parquet + CSV, **CC-BY-4.0**). `episodes`: episode_id, parameter set, t0, t1_first, t1_last, span, direction, clock move, `long_cluster`, `truncated_by_next`, `pre_in_prior_recovery`, incentive/structural flags. `pool_episode_depth`: per minute D_0.005, D_0.01, D_0.02, ∫L, p, block. `pool_episode_summary`: D_pre, D_min, `dip`, `local_shock`, `outage`, `chain_degraded`, `header_gap`, `band_reread_count`, `band_read_failed`, `pool_absent`, T_rec, `censored`, T_50, replenishment share. `pools`: §3 table with orientation and spacing. Flag names are exactly these everywhere.
2. **`resilience_score.py`** (**MIT**): chain + pool + episodes → T_rec, T_50, `dip`, replenishment share, resilience score = median T_rec with bootstrap CI; the §7.5 and §7.9 statistics with unit tests. Dependencies: requests, numpy, pandas, pyarrow, scipy; versions pinned in `requirements.lock` at code freeze. Nothing installed until the operator clears stage A (after [REDACTED-PROJECT] is done); stage A runs on the pinned versions.
3. **Short public report** (≤ 6 pages): question, manifest hashes, pools, episode table, three confirmatory results with verdicts, observed SD and power note, sensitivity table, confounds, what would change the conclusion, links.

---

## 10. Pre-freeze record and freeze protocol

### 10.1 Feasibility (done; deviation recorded)
A CEX proxy count (Binance ETHUSDT 1-minute closes, checksums verified, same rules) gave N = 47 at (0.05, 60) and 101 at (0.03, 30) (RUNLOG 17 Oct 2026). The binding decision is §5.6, taken after freeze from pool-clock prices only.

### 10.2 Incentive programs, structural events, and the S4 outage list

| # | Event | Window | Chains / pools | Flag rule |
|---|---|---|---|---|
| I1 | Gauntlet 2023–24 Uniswap Arbitrum liquidity-mining program (1.8M ARB, Merkl); WETH/USDC 0.05% proposed as a target | ≈ Oct 2023 → ≈ 24 Jul 2024 `[VERIFY]` | Arbitrum v3 | Arbitrum pool-episodes with t0 < 2024-07-24 → `incentive_possible` |
| I2 | Arbitrum LTIPP, Uniswap allocation (1M ARB + 100K UNI, Merkl, 30 v3 pools) | 2024-06-17 → ≈ 2024-09-09 `[VERIFY pools]` | Arbitrum v3 | Arbitrum pool-episodes with t0 in [2024-06-17, 2024-09-09] → `incentive_possible` |
| I3 | "Uniswap Unleashed" v4 incentives on Ethereum, Arbitrum, Base (~$24M, ~6 months, blue-chip pools, Merkl) | vote closed 2025-03-19; ≈ Apr–Sep 2025 `[VERIFY dates on Merkl]` | pulls LPs out of the v3 study pools | all pool-episodes with t0 in [2025-03-19, 2025-09-30] → `incentive_v4_pull` |
| I4 | Unichain liquidity program (Gauntlet + UF, $21.8M, v4 pools incl. USDC/ETH) | 2025-04-14 → ≤ 2026-04-14 `[VERIFY end]` | Unichain (exploratory) | → `incentive_unichain` |
| I5 | Base onboarding package / Merkl case study | most likely 2023; no 2024–26 Base ETH/USDC v3 campaign found `[VERIFY on Merkl history]` | Base v3 | no flag |
| I6 | Arbitrum DRIP | Sep 2025 → 2026 | lending only | no flag |
| S1 | Uniswap v4 launch | 2025-01-31 | all | no flag (covered by I3) |
| S2 | UNIfication fee switch wave 1 (all v2, selected v3; protocol share 25% of the swap fee on 0.01%/0.05%, ~16.7% on 0.3%/1%) | 2025-12-28 `[VERIFY wave-1 v3 list]` | Ethereum at least | pool-episodes with t0 ≥ 2025-12-28 → `structural_fee_switch` |
| S3 | Fee switch wave 2 (all v3 on Ethereum; v2/v3 on Arbitrum, Base, six others) | ≈ 2026-07-27 `[VERIFY on Tally 99/100]` | all three chains | t0 ≥ 2026-07-27 additionally → `structural_fee_switch_all` |
| **S4** | **Sequencer / block-production incident list**: `outages/outage_list_20261017.md` (sha256 `2f6115aa761b4643118442d019db1c922c2541e1d8d10c5af325501366c9d5e4`, identical for stored and LF-normalised bytes; also in RUNLOG). Halts: Base 2025-08-05 06:07–06:40; Base 2026-06-25 15:47–17:58; Base 2026-06-26 15:28–15:48; Unichain 2025-10-03 18:33–19:42 (timezone `[VERIFY]`, exploratory pool only). Degraded: Base 2026-05-13 16:44 → 05-14 05:04; Base 2026-02-05 15:22 → 02-06 01:46. Arbitrum One and Ethereum: none in the window. | as listed | Base, Unichain | halts → `outage` (drop chain-episode); degraded → `chain_degraded` (flag) |

The fee switch takes a larger share from the 0.05% tier than from the 0.3% tier, so the tier contrast is also reported pre/post switch (descriptive). Proxy-count overlap (47 episodes at (0.05, 60)): I3 contains 10; S2 contains 8; S3 contains 3; I2 contains 4 (Arbitrum); I1 contains 10 (Arbitrum, possible).

### 10.3 Freeze protocol and manifest
**Two freezes.**
- **Prereg freeze (now, on the operator's ruling after the targeted check):** RUNLOG gets a "FREEZE v0.3" entry listing, for each file below, the sha256 of its bytes as stored and the sha256 of its LF-normalised content (CRLF → LF, no trailing-whitespace change): `PREREG_uniswap_recovery_v0.3.md`; `outages/outage_list_20261017.md`; `probes/probe_rpc.py`, `probes/probe_rpc_alt.py`, `probes/probe_20261017.json`, `probes/probe_alt_20261017.json`, `probes/probe_drpc_20261017.txt`, `probes/probe_genesis_20261017.txt`; `proxy/proxy_episode_count.py`, `proxy/proxy_summary.json`; `bigquery/feasibility_episode_count.sql` (record only). **RUNLOG itself is hashed at the next entry** (its hash at freeze time is written into the entry that follows the freeze entry), so the freeze entry is never self-referential. If the folder is placed under git, the commit hash is added to the next RUNLOG entry; the sha256 list is the anchor either way.
- **Code freeze (after stage A passes, before the price-only pull):** RUNLOG gets a "CODE FREEZE" entry with the same two hashes for every pipeline file (`price_series.py`, `pull_logs.py`, `reconstruct.py`, `depth.py`, `episodes.py`, `resilience_score.py`, `analysis.py`), their unit tests, and `requirements.lock` (exact Python, numpy, pandas, pyarrow, scipy, requests versions).

Checklist:
- [x] Proxy feasibility recorded as a deviation (§10.1).
- [x] Incentive and structural-event list (§10.2) with five `[VERIFY]` boundaries fixed as written.
- [x] S4 outage list compiled and saved (`outages/outage_list_20261017.md`); one `[VERIFY]` (Unichain timezone) fixed as written.
- [x] OP-stack timestamp formula verified on genesis and two recent blocks per chain.
- [x] Rulings taken: R2 primary with R1 cross-check; one statistic per episode; long clusters kept and flagged; price-only threshold rule; Base via drpc windows; S-i exploratory; two check stages with constrained fixes.
- [x] **All `[VERIFY]` items are explicitly frozen as written** (I1–I5, S2, S3, S4 timezone, Base drpc archive depth); moving any after freeze is a logged deviation, not a version bump.
- [x] Targeted fresh-eyes check of the 4 blocking + 3 insisted should-fix items: all seven reported LANDED, nothing blocking (17 Oct 2026); its three non-blocking wording suggestions applied (§5.6 step 3 / §6.6 timing of the formula check; §9 install wording; S4 hash written into §10.2).
- [x] Prereg freeze: this file hashed and the "FREEZE v0.3" entry written in RUNLOG on 2026-10-17.
- [ ] (later) Pipeline written; stage A passes; code freeze entry written.

---

## 11. Probe log
See v0.1 §11 and `probes/`. 106 tiny JSON-RPC requests on 17 Oct 2026 (pool verification, endpoint caps) plus 8 header reads for the timestamp-formula check (`probes/probe_genesis_20261017.txt`).

---

## 12. Open items
None requiring a ruling. `[VERIFY]` items are frozen as written (§10.3).

---

## 13. Work log
- 17 Oct 2026: v0 drafted; rulings 1–12 and v0.1; probes; BigQuery dry run (1.51 TB, not executed); CEX proxy count (47 / 101); incentive list; first fresh-eyes review (25 findings) → v0.2; second fresh-eyes review (20 findings, v0.2 §14).
- 17 Oct 2026 (v0.3): operator accepted G4 and G12 and the two-stage check order; outage list compiled from status pages and post-mortems; OP-stack timestamp formula verified with 8 header reads; every Section-14 item applied; this file written as a new version; v0, v0.1, v0.2 untouched.
- 17 Oct 2026 (freeze): targeted fresh-eyes check reported G1–G7 all landed and nothing blocking; three non-blocking wording edits applied; file marked frozen and hashed; RUNLOG "FREEZE v0.3" entry written.
- Not done: no on-chain bulk download, no install, no pipeline code.

UNISWAP PREREG v0.3 FROZEN — NOTHING RUN
