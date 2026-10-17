# SPEC_NOTES — ambiguities met while coding against frozen prereg v0.3 (17 Oct 2026)

Rule: the prereg is frozen; nothing here changes it. Each note records the ambiguity, the reading the code uses, and
whether it could matter. To be ruled on at the code freeze (v0.3 §10.3) and logged as deviations if the reading changes.

| # | Section | Ambiguity | Reading implemented | Could it bend a result? |
|---|---|---|---|---|
| N1 | 6.3 | "Censored at 4,320 min": is a recovery whose first qualifying minute is exactly t1_last + 4,320 censored or observed? | Observed T_rec must be < censor_min; T_rec ≥ censor_min is censored (value = censor_min). `recovery_time` searches t in [t1_last, t1_last + censor_min − 1]. | Edge case only; symmetric for all pools. |
| N2 | 6.3 | Persistence window near the horizon: the 15-minute check for t = t1_last + 4,319 needs samples to +4,333, which 5.3 provides (+72 h + 15 min). | Implemented as specified; a missing minute inside the persistence window counts as a failure, not as carried-forward. | Only if depth samples are missing, which 6.2 forbids (state carries forward). |
| N3 | 6.1 | "Initialised tick" after replay: a tick whose liquidityNet returns to 0 is treated as uninitialised. | `_modify` deletes zero entries; `initialised_ticks` ignores zeros. | No. |
| N4 | 6.1 | Active-liquidity check at a Swap when the price leaves the read band inside a block, and for later swaps in the same block (even ones that return inside the band). | **Updated after stage A (D6):** once any swap leaves the band the state is `band_dirty`; every later swap in that block adopts the event's liquidity without the equality check; the block-end re-read is forced; the flag clears on the re-read. Without this, a same-block exit-and-return walks across ticks the snapshot never read (seen on 2023-01-14, block 16,400,794). | Equality is unchecked for the rest of that block only; re-reads are counted and logged. |
| N5 | 6.2 | ∫L "over the ±Z tick band": units. | Σ L × (tick width in ticks) over the same pieces used for D_Z. | Secondary only. |
| N6 | 6.2 | The Z band must lie inside the read band; near the band edge (6–8%) the primary Z = 1% band fits but Z = 2% may not before a re-read. | `depth` raises if the band is not covered; the caller samples only after the block-end re-read rule has run, so this cannot happen for Z ≤ 2% after a re-read at 6%. | No. |
| N7 | 7.4 | Rank-only replacement when d_val is exactly 0 (sign 0). | Dropped and counted as a zero. | Stated in 7.4; no. |
| N8 | 7.4 / 7.6 | Bootstrap for M uses per-episode means of r over all valid pairs (censored at value); the same values define the median ratio. | Implemented (`pairs_r` per episode). | No. |
| N9 | 7.5 | scipy is named for the frozen test; not installed on this machine. | Exact pure-Python fallback with identical conventions; `SCIPY_AVAILABLE` recorded; a test compares both when scipy exists. **Install of scipy needs the operator's ruling before stage A.** | p-values identical in the exact regime; normal-approximation regime may differ in the last digits. |
| N10 | 7.8 S-b | "On Arbitrum additionally exclude incentive_possible" — exclude the Arbitrum pairs of flagged episodes, or the whole episodes? | Arbitrum pairs only; other chains' pairs of the same episode stay. | Sensitivity only. |
| N11 | 7.8 S-c | dip = false pool-episodes "enter with T_rec = 0": their censored flag? | Treated as uncensored with T_rec = 0. | Sensitivity only. |
| N12 | 5.5 | Local confirmation when p(t0) or p(t1_first) cannot be read (slot0 call fails). | `local_shock = false` for that pool-episode (counted). | Conservative; could lower N_conf. |
| N13 | 5.1 | Forward fill before the first observation in the series. | NaN; triggers cannot fire until L minutes after the first observation. | Only at the very start of the series. |
| N14 | 6.5 | Header-gap flag needs two fetched headers adjacent by block number; with log-bearing blocks only this rarely occurs. | Implemented literally (`header_gaps`); flag reported, never dropped. | No (reporting only). |
| N15 | 7.9 | W_s is the sum of positive signed ranks (not the min of the two sums). | Implemented as the positive-rank sum, centred by n(n+1)/4. | Secondary only. |
| N16 | 6.4 | Replenishment share not implemented in this draft (descriptive only; NFPM owner limitation stated in the prereg). | Field present, value None. | No (descriptive). |
| N17 | 8.2 | Base price-only phase: p(t0) and p(t1_first) come from two slot0 reads; minute-to-block mapping by formula. | `block_at_minute_end`; verified formula. | No. |
| N18 | 5.2 | `first_dlog` is the 60-min log change at t1_first, which equals log(p(t1_first)/p(t0)) by construction (t0 = t1_first − L); direction uses it. | Identity holds because t0 is defined from the same lag. | No. |

Status: tests are synthetic only; no real chain data has been touched; no package installed.

## Plain-words summary (requested 17 Oct 2026) — what each note means and how the code resolves it

| # | In plain words | How the code resolves it |
|---|---|---|
| N1 | Is a recovery that happens exactly at the 72-hour mark observed or censored? | Censored. `recovery_time` searches minutes t1_last … t1_last + censor − 1 only; anything later is censored at the censor value. |
| N2 | The 15-minute persistence test near the end needs samples past 72 h; a missing minute inside the test window. | Series run to 72 h + 15 min (§5.3); a missing minute fails the persistence test rather than being carried forward (`_first_persistent`). |
| N3 | A tick whose net liquidity returns to zero: still "initialised"? | No. `_set_net` removes zero entries, so the depth walk ignores it (same as the chain, which keeps it in the bitmap but with zero net). |
| N4 | What to do with the liquidity check when the price leaves the read band inside a block, or comes back in the same block. | The state is marked dirty (`band_dirty`); later swaps in that block adopt the event's liquidity without the check; a re-read is forced at block end; depth is sampled only after it (D6). |
| N5 | Units of the secondary "integrated liquidity" measure. | Sum of liquidity × tick-width over the same pieces used for D_Z (`depth.int_l`). Secondary only. |
| N6 | The depth band must sit inside the read band. | `depth` raises if not; sampling happens only after the block-end re-read rule, and the re-read trigger (6%) leaves room for Z ≤ 2% inside the 8% band. |
| N7 | A rank-only episode whose value-based mean is exactly zero. | Dropped from the signed-rank test and counted as a zero (`episode_stats`, `run_test`). |
| N8 | Which numbers feed the bootstrap and the median ratio. | The value-based per-episode means over all valid pairs, censored members at their censored value (`pairs_r`, `d_val`). |
| N9 | scipy is named by the prereg but was not installed. | Installed in the project venv (1.13.1); `wilcoxon_signed_rank(impl="scipy")` is the default; a pure-Python exact fallback exists and a test shows both agree. |
| N10 | Sensitivity S-b: exclude whole episodes or only the Arbitrum pairs? | Only the Arbitrum pairs of flagged episodes (`run_sensitivities`, `res_b`). |
| N11 | In sensitivity S-c, is a no-dip pool-episode censored? | Treated as uncensored with T_rec = 0 (`res_c`). |
| N12 | A slot0 price read fails in the price-only phase. | That pool-episode gets local_shock = false and is counted (`local_flags`). |
| N13 | Prices before the first swap of the series. | NaN; no trigger can fire until one lag after the first observation (`forward_fill`, `trigger_minutes`). |
| N14 | The header-gap flag needs two fetched headers adjacent by block number. | Implemented literally (`header_gaps`); reporting only, never drops. |
| N15 | The stratified statistic's W_s. | Sum of positive signed ranks, centred by n(n+1)/4 (`stratified_signed_rank`). Secondary only. |
| N16 | Replenishment share. | Not computed in this code (field stays None); descriptive only per the prereg; NFPM-owner limitation noted. |
| N17 | Base price-only reads. | Two archive `slot0` reads per pool-episode at blocks found by the verified OP-stack formula (`block_at_minute_end`, `decode_slot0`). |
| N18 | Direction uses the 60-minute change at the first trigger. | Identical to log(p(t1_first)/p(t0)) because t0 = t1_first − L (`chain_triggers`, `first_dlog`). |

## Updates at the code freeze (17 Oct 2026, operator ruling on the should-fix items)

| # | Section | Ambiguity or change | Reading implemented | Could it bend a result? |
|---|---|---|---|---|
| N4 (updated) | 6.1 | After a same-block band exit and return, D6 skipped the equality check for the rest of the block. | **R15 / D8:** once the returning swap has adopted the event's liquidity, later in-band swaps in the same block are checked again; `band_dirty` still forces the block-end re-read. Counters `swaps_checked` / `swaps_unchecked` are kept on every state and reported per pool-episode. | Tightness only (more checks, never fewer). |
| N6 (updated) | 6.1 / 6.2 | Near the band edge the Z = 2% band may leave the ±8% read band while the price is still inside ±6%. | **D7:** the block-end re-read also fires when the ±2% band around the block-end price is not inside the read band (`reread_reason() == "z_edge"`); each Z is sampled in its own try, so only a secondary Z can be missing, never Z = 1%. | More re-reads, never fewer; sample values unchanged. |
| N9 (updated) | 7.5 | scipy. | Mandatory: `run_confirmatory` refuses to run without scipy 1.13.1 (`requirements.lock`); the pure-Python fallback remains for the comparison test only. | No. |
| N19 | 5.6 / 4 | "Swap logs over W plus margins": the margins are not sized in the prereg. | Series = [W_start − 1 day, W_end + 7 days): one lag plus forward fill before W; chaining (48 h) and the §5.4 truncation flag of the last episode after W (`chains.py`). Episodes are confirmatory iff t1_first ∈ W, as written. | No: the margins only make the rules computable at the edges of W. |
| N20 | 6.5 / 7.1a | Pool-episodes dropped by the outage rule or by `pool_absent` (creation block > b0): pulled anyway? | Not pulled; a flagged record is emitted for every pool-episode (R14) so the tally reason is `outage` / `pool_absent`, never a silent gap. | No (they never enter a pair). |
| N21 | 6.1 | R1 for the confirmatory phase: "full Mint/Burn replay since pool creation" gives the net map; the price at b0 is needed too. | R1 state at b0 = lifetime net map (every Mint/Burn ≤ b0) + `slot0(b0)`; active L = Σ net over ticks ≤ tick, cross-checked against `liquidity(b0)`; then every window swap is checked (`net_map_from_events`, `state_from_net_map`). Stage A replayed the 2023 swaps too; both give the same state (test). | No. |
| N22 | 8.4 | Completeness check when the second source cannot serve a block (recent-only endpoints). | Counted as `unavailable`, reported; a differing count is the hard failure. | No (reporting). |
| N23 | 8.2 | Blockscout fallback returns logs without a canonical `blockHash` field in some versions. | `blockHash` filled from the block number for dedupe keys ((block, logIndex) is equivalent below the cutoff); timestamps on Base come from the formula anyway. | No. |
| N24 | 6.6 (D9) | Ethereum timestamps from the `blockTimestamp` field of `eth_getLogs` instead of headers (ruling 17 Oct 2026, item 3). | Clock series: 1% of each 10k-block chunk's log-bearing blocks (at least 50 per chunk) compared with `eth_getBlockByNumber`; depth windows: 1%, at least 200 per pool-episode window; any mismatch stops the driver (`--eth-timestamps headers` is the §6.6 route). Arbitrum stays on headers (field unpopulated), Base/Unichain on the formula. | No if the check passes; the source is the same endpoint's own block timestamp. |
| N25 | 8.2 | Endpoints are operational parameters. | `chains.py` holds the probed defaults; `data/confirmatory/routes_override.json` (recorded in RUNLOG at pull time) can change URLs, caps and batch sizes without touching the frozen code. | No. |
