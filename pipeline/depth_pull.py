"""Depth pull driver (prereg v0.3 sections 5.3, 6.1-6.6, 7.1a, 8.2, 8.4; deviations D8, D9, D12, D13). Runs only after the
price-only decision exists and the operator's GO; one chain per invocation.

Per pool-episode: b0 and b_end by block location (6.6), the window's Mint/Burn/Swap logs with provenance
(window_pull), the R2 snapshot route (band read at b0, replay with re-reads, end-of-minute sampling) for the depth series,
and on Ethereum/Arbitrum the R1 route (lifetime Mint/Burn net map + slot0 at b0) for the Swap-liquidity equality check on
every swap and the R1/R2 series comparison (stage B, D8). Base/Unichain report swaps_checked/swaps_unchecked counters.
Every pool-episode gets a record, flagged or not (R14); outage and pool_absent pool-episodes are recorded without a pull.
Checks per chain (stage B (i)/(ii), formula verification, two-source completeness, header gaps) go to checks_<chain>.json;
run_analysis refuses to run unless every chain's checks pass.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from . import loadguard
from .band_read import SEL_LIQ, SEL_SLOT0, archive_calls, band_read
from .blocks import BlockLocator, HeaderCache, sample_blocks_in_windows, verify_formula_on, window_block_range
from .chains import ROUTES, ROUTES_OVERRIDE_FILE, SEED, Live, build_live, load_routes, pools_on, with_timestamps
from .common import BURN_TOPIC, CHAIN_TIME, MINT_TOPIC, POOLS, PoolSpec, parse_int24
from .episodes import Episode
from .flags import outage_flags
from .pull_logs import RpcError, completeness_check, cutoff, decode_log, dedupe, iter_ranges
from .reconstruct import (Event, PoolState, ReconstructionError, net_map_from_events, replay_window, state_from_net_map,
                          state_from_snapshot)
from .resilience_score import evaluate
from .sampling import Clock, WindowSampler, Z_PRIMARY_INDEX
from .transport import Stats, get_logs_split, log, retry, utc_now
from .window_pull import CSV_COLS, event_row, pool_dir_name, pull_window, read_events, window_dir

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "confirmatory" / "depth"
DECISION_DEFAULT = ROOT / "data" / "confirmatory" / "price_only" / "decision.json"
STAGE_A_LOGS = ROOT / "data" / "stageA" / "logs"
STAGE_A_INIT = ROOT / "data" / "stageA" / "init.json"
STAGE_A_BOUNDS = ROOT / "data" / "stageA" / "bounds.json"
LO_GUESS = {"ethereum": 18_800_000, "arbitrum": 150_000_000}
R1_CHAINS = ("ethereum", "arbitrum")
TOL = 0.001
DRAW_N = 5
COMPLETENESS_N = 50
FORMULA_N = 50
MINTBURN_TOPICS = [[MINT_TOPIC, BURN_TOPIC]]


def _retry_raise(fn, what):
    return retry(fn, what, attempts=2, give_up="raise")


@dataclass
class Context:
    out: Path
    chain: str
    live: Live
    episodes: List[Episode]                                     # the chosen set (5.6), confirmatory episodes only
    local_flags: Dict[int, Dict[str, bool]]                     # episode_id -> {pool_key: local_shock}
    lo_guess: int = 0
    lifetime_seed_dirs: Dict[str, List[Path]] = field(default_factory=dict)   # earlier Mint/Burn chunks (stage A)
    lifetime_from: Dict[str, int] = field(default_factory=dict)               # first block of the own lifetime pull
    creation_override: Dict[str, int] = field(default_factory=dict)
    run_r1: bool = True

    @property
    def pools(self) -> List[PoolSpec]:
        return pools_on(self.chain)


def draw_five(episodes: List[Episode]) -> List[int]:
    """Stage B (ii): random.Random(20261017).sample(sorted(confirmatory_episodes, key=t1_first), 5) (6.1)."""
    pick = random.Random(SEED).sample(sorted(episodes, key=lambda e: e.t1_first), min(DRAW_N, len(episodes)))
    return sorted(e.episode_id for e in pick)


# ----------------------------------------------------------------------------- creation blocks, lifetime Mint/Burn
def find_creation_block(transport, address: str, lo: int, hi: int) -> int:
    """First block at which the pool contract has code (eth_getCode bisection on an archive endpoint)."""
    def has_code(n: int) -> bool:
        r = retry(lambda: transport.call("eth_getCode", [address, hex(n)]), f"getCode {address[:10]} {n}")
        return bool(r) and r != "0x"
    if has_code(lo):
        return lo
    if not has_code(hi):
        raise SystemExit(f"ERROR {address} has no code at block {hi}")
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if has_code(mid):
            hi = mid
        else:
            lo = mid
    return hi


def phase_creation(ctx: Context, cutoff_block: int) -> Dict[str, int]:
    out = {}
    stage_a = json.loads(STAGE_A_INIT.read_text(encoding="utf-8")) if STAGE_A_INIT.is_file() else {}
    for pool in ctx.pools:
        f = ctx.out / f"creation_{pool_dir_name(pool)}.json"
        if f.exists():
            out[pool.key] = json.loads(f.read_text(encoding="utf-8"))["creation_block"]
            continue
        if pool.key in ctx.creation_override:
            blk, src = ctx.creation_override[pool.key], "override"
        elif pool.key in stage_a:
            blk, src = stage_a[pool.key]["block"], "stage A Initialize event"
        else:
            blk, src = find_creation_block(ctx.live.calls, pool.address, 0, cutoff_block), "eth_getCode bisection"
        f.write_text(json.dumps({"pool": pool.key, "creation_block": blk, "source": src}, indent=1), encoding="utf-8")
        out[pool.key] = blk
        log(f"creation {pool.key}: block {blk} ({src})")
    return out


def pull_lifetime(ctx: Context, pool: PoolSpec, from_block: int, to_block: int) -> Path:
    """Lifetime Mint/Burn chunks (R1) under lifetime/<pool>/mintburn/, resumable by chunk file."""
    d = ctx.out / "lifetime" / pool_dir_name(pool) / "mintburn"
    d.mkdir(parents=True, exist_ok=True)
    cap = ctx.live.route.log_cap
    for a, b in iter_ranges(from_block, to_block, cap):
        f = d / f"{a:010d}-{b:010d}.csv"
        if f.exists():
            continue
        raw = retry(lambda: get_logs_split(ctx.live.logs, pool.address, MINTBURN_TOPICS, a, b), f"lifetime {pool.key} {a}-{b}")
        rows = [event_row(decode_log(lg)) for lg in dedupe(raw)]
        tmp = f.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLS)
            w.writerows(rows)
        tmp.replace(f)
        Stats.gate()
    return d


def lifetime_events(ctx: Context, pool: PoolSpec) -> Iterator[Event]:
    dirs = list(ctx.lifetime_seed_dirs.get(pool.key, [])) + [ctx.out / "lifetime" / pool_dir_name(pool) / "mintburn"]
    for d in dirs:
        for f in sorted(Path(d).glob("*.csv")):
            yield from read_events(f)


# ----------------------------------------------------------------------------- replay routes
def grouped_by_block(events: Iterator[Event]) -> Iterator[List[Event]]:
    group: List[Event] = []
    for ev in events:
        if group and ev.block != group[0].block:
            yield group
            group = []
        group.append(ev)
    if group:
        yield group


def run_r2(ctx: Context, pool: PoolSpec, ep: Episode, b0: int, clock: Clock, events_path: Path) -> dict:
    batch = ctx.live.route.call_batch
    try:
        snap = _retry_raise(lambda: band_read(ctx.live.calls, pool, b0, batch, _retry_raise), f"band read {pool.key} ep{ep.episode_id} b0")
    except RpcError as e:
        return {"band_read_failed": True, "error": f"snapshot at b0 failed: {str(e)[:120]}", "sampler": None, "state": None}
    Stats.band_reads += 1
    state = state_from_snapshot(pool, snap)
    sampler = WindowSampler(ep.window_start, ep.window_end, clock, b0)

    def reader(block: int):
        Stats.band_reads += 1
        return band_read(ctx.live.calls, pool, block, batch, _retry_raise)

    error = None
    try:
        for group in grouped_by_block(read_events(events_path)):
            sampler.before_block(group[0].block, state)
            replay_window(state, group, reader=reader)
            if state.band_read_failed:
                break
        if not state.band_read_failed:
            sampler.finish(state)
    except ReconstructionError as e:
        error = str(e)[:200]
    return {"band_read_failed": state.band_read_failed, "error": error, "sampler": sampler, "state": state}


def slot0_and_liquidity(ctx: Context, pool: PoolSpec, block: int) -> Tuple[int, int, int]:
    res = archive_calls(ctx.live.calls, pool, [SEL_SLOT0, SEL_LIQ], hex(block), ctx.live.route.call_batch, "slot0+liquidity", retry)
    s0 = res[0][2:]
    return int(s0[:64], 16), parse_int24(s0[64:128]), int(res[1][2:66], 16)


def run_r1(ctx: Context, pool: PoolSpec, ep: Episode, b0: int, clock: Clock, events_path: Path) -> dict:
    nets = net_map_from_events(lifetime_events(ctx, pool), pool, b0)
    sqrt_p, tick, liq_b0 = slot0_and_liquidity(ctx, pool, b0)
    state = state_from_net_map(pool, nets, sqrt_p, tick)
    liq_ok = state.liquidity == liq_b0
    sampler = WindowSampler(ep.window_start, ep.window_end, clock, b0)
    n_swaps = n_mism = 0
    first: List[str] = []
    for group in grouped_by_block(read_events(events_path)):
        sampler.before_block(group[0].block, state)
        for ev in group:
            if ev.kind == "swap":
                n_swaps += 1
            try:
                state.apply(ev)
            except ReconstructionError as e:
                n_mism += 1
                if len(first) < 5:
                    first.append(str(e)[:160])
                state.liquidity = ev.liquidity
                state.sqrt_price_x96, state.tick = ev.sqrt_price_x96, ev.tick
    sampler.finish(state)
    return {"sampler": sampler, "swaps": n_swaps, "mismatches": n_mism, "first_mismatches": first,
            "liquidity_check_ok": liq_ok, "liquidity_net_map": state.liquidity if liq_ok else None, "liquidity_b0": liq_b0,
            "initialised_ticks": len(nets)}


def compare_series(s1: Dict[int, list], s2: Dict[int, list]) -> dict:
    """Max relative difference per Z over the common minutes (stage B (ii), tolerance 0.1%)."""
    max_rel = [0.0, 0.0, 0.0]
    n = none = 0
    for m, a in s1.items():
        b = s2.get(m)
        if b is None:
            continue
        n += 1
        for z in range(3):
            if a[z] is None or b[z] is None:
                none += 1
                continue
            if a[z] > 0:
                max_rel[z] = max(max_rel[z], abs(b[z] - a[z]) / a[z])
    return {"minutes_compared": n, "max_rel_diff_by_Z": max_rel, "missing_secondary_samples": none,
            "pass": n > 0 and none == 0 and all(r <= TOL for r in max_rel)}


# ----------------------------------------------------------------------------- records
def metrics_for(series: Dict[int, Optional[float]], ep: Episode, pool: PoolSpec, flags: dict,
                creation_block: int, b0: int) -> Optional[dict]:
    if any(v is None for v in series.values()):
        return None
    try:
        r = evaluate(series, ep, pool.key, creation_block=creation_block, b0=b0, **flags)
    except ValueError:
        return None
    return {"d_pre": r.d_pre, "d_min": r.d_min, "dip": r.dip, "t_rec": r.t_rec, "censored": r.censored, "t50": r.t50,
            "pool_absent": r.pool_absent}


def base_record(pool: PoolSpec, ep: Episode, status: str, flags: dict, b0: Optional[int], b_end: Optional[int]) -> dict:
    return {"pool_key": pool.key, "episode_id": ep.episode_id, "status": status, "route": "R2", "b0": b0, "b_end": b_end,
            "d_pre": float("nan"), "d_min": float("nan"), "dip": False, "t_rec": ep.censor_min, "censored": True, "t50": None,
            "local_shock": flags["local_shock"], "outage": flags["outage"], "chain_degraded": flags["chain_degraded"],
            "header_gap": flags["header_gap"], "band_reread_count": flags["band_reread_count"],
            "band_read_failed": flags["band_read_failed"], "pool_absent": status == "pool_absent", "rereads": [],
            "r2_swaps_checked": 0, "r2_swaps_unchecked": 0, "r1": None, "r1_r2": None, "metrics_by_z": {}, "series_file": None,
            "error": None}


def pool_episode(ctx: Context, pool: PoolSpec, ep: Episode, b0: int, b_end: int, creation_block: int,
                 header_cache: HeaderCache) -> dict:
    chain_flags = outage_flags(ep, ctx.chain)
    flags = {"local_shock": True if pool.clock else bool(ctx.local_flags.get(ep.episode_id, {}).get(pool.key, False)),
             "outage": chain_flags["outage"], "chain_degraded": chain_flags["chain_degraded"], "header_gap": False,
             "band_reread_count": 0, "band_read_failed": False}
    if flags["outage"]:                                   # 6.5: both pools dropped; no pull (N20)
        return base_record(pool, ep, "outage", flags, b0, b_end)
    if creation_block > b0:                               # 7.1a pool_absent; no pull
        return base_record(pool, ep, "pool_absent", flags, b0, b_end)
    meta = pull_window(ctx.live, pool, ep.episode_id, b0, b_end, ctx.out / "windows", header_cache)
    flags["header_gap"] = bool(meta.header_gaps)
    d = window_dir(ctx.out / "windows", pool, ep.episode_id)
    z = np.load(d / "timestamps.npz")
    clock = Clock(z["blocks"], z["timestamps"])
    r2 = run_r2(ctx, pool, ep, b0, clock, d / "logs.csv")
    rec = base_record(pool, ep, "ok", flags, b0, b_end)
    rec.update({"events": meta.events, "log_bearing_blocks": meta.log_bearing_blocks, "sources": meta.sources,
                "ts_check": meta.ts_check, "header_gaps": meta.header_gaps})
    st: Optional[PoolState] = r2["state"]
    if st is not None:
        rec.update({"band_reread_count": st.band_reread_count, "rereads": st.rereads, "r2_swaps_checked": st.swaps_checked,
                    "r2_swaps_unchecked": st.swaps_unchecked})
        flags["band_reread_count"] = st.band_reread_count
    if r2["band_read_failed"] or r2["error"]:
        rec.update({"status": "band_read_failed" if r2["band_read_failed"] else "r2_error", "band_read_failed": True,
                    "error": r2["error"]})
        flags["band_read_failed"] = True
    sampler = r2["sampler"]
    if sampler is not None and sampler.complete and not flags["band_read_failed"]:
        sdir = ctx.out / "series" / pool_dir_name(pool)
        sdir.mkdir(parents=True, exist_ok=True)
        sf = sdir / f"ep{ep.episode_id}.npz"
        np.savez(sf, **sampler.arrays())
        rec["series_file"] = str(sf.relative_to(ctx.out))
        prim = metrics_for(sampler.primary_series(), ep, pool, flags, creation_block, b0)
        if prim is None:
            rec.update({"status": "series_incomplete", "band_read_failed": True})
        else:
            rec.update(prim)
            for zi, name in ((0, "0.005"), (2, "0.02")):
                rec["metrics_by_z"][name] = metrics_for(sampler.series_for_z(zi), ep, pool, flags, creation_block, b0)
    if ctx.run_r1 and ctx.chain in R1_CHAINS:
        r1 = run_r1(ctx, pool, ep, b0, clock, d / "logs.csv")
        rec["r1"] = {k: v for k, v in r1.items() if k != "sampler"}
        if sampler is not None and sampler.complete:
            rec["r1_r2"] = compare_series(r1["sampler"].series, sampler.series)
    return rec


# ----------------------------------------------------------------------------- checks (stage B and 8.4)
def phase_checks(ctx: Context, records: List[dict], windows: Dict[int, Tuple[int, int]], cutoff_block: int,
                 draw_ids: List[int]) -> dict:
    f = ctx.out / f"checks_{ctx.chain}.json"
    ok_recs = [r for r in records if r["status"] == "ok"]
    out: dict = {"chain": ctx.chain, "at_utc": utc_now(), "pool_episodes": len(records),
                 "status_counts": {s: sum(1 for r in records if r["status"] == s) for s in sorted({r["status"] for r in records})}}
    if ctx.chain in R1_CHAINS:
        bad = [(r["pool_key"], r["episode_id"]) for r in ok_recs if r["r1"] is None or r["r1"]["mismatches"] or not r["r1"]["liquidity_check_ok"]]
        out["stage_b_i"] = {"route": "R1 full Mint/Burn replay + slot0 at b0", "swaps_checked": sum(r["r1"]["swaps"] for r in ok_recs if r["r1"]),
                            "failures": bad, "pass": not bad}
        drawn = [r for r in ok_recs if r["episode_id"] in draw_ids]
        bad2 = [(r["pool_key"], r["episode_id"], r["r1_r2"]) for r in drawn if not (r["r1_r2"] and r["r1_r2"]["pass"])]
        out["stage_b_ii"] = {"drawn_episode_ids": draw_ids, "pool_episodes_compared": len(drawn), "failures": bad2, "pass": not bad2,
                             "all_comparisons": {f"{r['pool_key']}#{r['episode_id']}": r["r1_r2"] for r in ok_recs if r["r1_r2"]}}
    else:
        bad = [(r["pool_key"], r["episode_id"], r["error"]) for r in records if r["status"] == "r2_error"]
        out["stage_b_i"] = {"route": "R2 snapshot route (D8: no R1 on this chain); equality checked on in-band swaps only",
                            "swaps_checked": sum(r["r2_swaps_checked"] for r in ok_recs),
                            "swaps_unchecked": sum(r["r2_swaps_unchecked"] for r in ok_recs),
                            "per_pool_episode": {f"{r['pool_key']}#{r['episode_id']}": [r["r2_swaps_checked"], r["r2_swaps_unchecked"]] for r in ok_recs},
                            "failures": bad, "pass": not bad}
        out["stage_b_ii"] = {"drawn_episode_ids": draw_ids, "note": "not applicable on this chain (D8): R1/R2 agreement is checked on the Ethereum/Arbitrum members", "pass": True}
    wins = [(max(a + 1, 0), min(b, cutoff_block)) for a, b in windows.values() if max(a + 1, 0) <= min(b, cutoff_block)]
    blocks = sample_blocks_in_windows(wins, COMPLETENESS_N, SEED) if wins else []
    if not wins:
        out["formula"] = {"ok": True, "sampled": 0, "note": "no episode window inside the chain's block range"}
    elif ctx.chain in CHAIN_TIME and ctx.live.route.timestamps == "formula":
        fb = sample_blocks_in_windows(wins, FORMULA_N, SEED)
        out["formula"] = retry(lambda: verify_formula_on(ctx.live.headers, ctx.chain, fb, ctx.live.route.header_batch), f"formula {ctx.chain}")
    else:
        out["formula"] = {"ok": True, "note": "headers route"}
    comp = {}
    if ctx.live.secondary is None or not blocks:
        comp["note"] = "no second source on this chain (8.4)" if ctx.live.secondary is None else "no blocks to sample"
        comp["ok"] = True
    else:
        for pool in ctx.pools:
            comp[pool.key] = retry(lambda p=pool: completeness_check(ctx.live.logs, ctx.live.secondary, p.address, blocks),
                                   f"completeness {pool.key}")
        comp["ok"] = all(v["ok"] for k, v in comp.items() if k != "ok")
    out["completeness"] = comp
    out["header_gaps"] = {f"{r['pool_key']}#{r['episode_id']}": r.get("header_gaps") for r in ok_recs if r.get("header_gaps")}
    out["pass"] = bool(out["stage_b_i"]["pass"] and out["stage_b_ii"]["pass"] and out["formula"]["ok"] and comp["ok"])
    f.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    log(f"checks {ctx.chain}: {'PASS' if out['pass'] else 'FAIL'}")
    return out


def write_pull_record(ctx: Context, cut: dict, records: List[dict], checks: dict, t_start: str) -> dict:
    rec = {"chain": ctx.chain, "endpoint": asdict(ctx.live.route), "started_utc": t_start, "finished_utc": utc_now(), "cutoff": cut,
           "pool_episodes": len(records), "status_counts": checks["status_counts"],
           "logs_raw": sum(r.get("events", 0) for r in records), "rereads": sum(r["band_reread_count"] for r in records),
           "checks_pass": checks["pass"], "stats": Stats.snapshot()}
    (ctx.out / f"pull_record_{ctx.chain}.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    rr = [f"  - {r['pool_key']} ep{r['episode_id']} block {x['block']} ({x['reason']}): centre {x['old_center_p']:.2f} -> {x['new_center_p']:.2f}"
          for r in records for x in r["rereads"]]
    lines = [f"## {rec['finished_utc']} — DEPTH PULL DONE: {ctx.chain}", "",
             f"- Endpoint logs `{ctx.live.route.logs_url}` (cap {ctx.live.route.log_cap}), calls `{ctx.live.route.calls_url}`, timestamps `{ctx.live.route.timestamps}`; "
             f"started {t_start}; cutoff block {cut['block']} hash {cut['hash']}.",
             f"- Pool-episodes {len(records)}: {checks['status_counts']}; band re-reads {rec['rereads']}; requests {Stats.calls}; "
             f"bytes {Stats.bytes_in / 2 ** 20:.0f} MB; retries {Stats.retries}; rate-limit hits {Stats.rate_limit_hits}; fallbacks {Stats.fallbacks}; "
             f"pauses {len(Stats.pauses)}; peak RSS {Stats.peak_rss_mb:.0f} MB.",
             f"- Stage B (i): {checks['stage_b_i'].get('route')} — {'PASS' if checks['stage_b_i']['pass'] else 'FAIL'}; "
             f"(ii): {'PASS' if checks['stage_b_ii']['pass'] else 'FAIL'} on drawn episodes {checks['stage_b_ii']['drawn_episode_ids']}; "
             f"formula {'OK' if checks['formula']['ok'] else 'MISMATCH'}; completeness {'OK' if checks['completeness']['ok'] else 'MISMATCH'}.",
             "- Band re-reads (pool-episode, block, reason, old -> new centre):"] + (rr or ["  - none"])
    (ctx.out / f"RUNLOG_depth_{ctx.chain}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rec


# ----------------------------------------------------------------------------- driver
def load_records(f: Path) -> List[dict]:
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(ctx: Context) -> dict:
    ctx.out.mkdir(parents=True, exist_ok=True)
    t_start = utc_now()
    cf = ctx.out / f"cutoff_{ctx.chain}.json"
    if cf.exists():
        cut = json.loads(cf.read_text(encoding="utf-8"))
    else:
        cut = retry(lambda: cutoff(ctx.live.headers), f"cutoff {ctx.chain}")
        cut.update({"chain": ctx.chain, "at_utc": utc_now()})
        cf.write_text(json.dumps(cut, indent=1), encoding="utf-8")
    creation = phase_creation(ctx, cut["block"])
    cache = HeaderCache.load(ctx.out / f"header_cache_{ctx.chain}.json")
    mode = "formula" if ctx.live.route.timestamps == "formula" else "headers"
    locator = BlockLocator(ctx.chain, mode, ctx.live.headers, cache, ctx.lo_guess, cut["block"])
    windows: Dict[int, Tuple[int, int]] = {}
    for ep in ctx.episodes:
        b0, b_end = window_block_range(locator, ep.window_start, ep.window_end)
        if b_end > cut["block"]:
            raise SystemExit(f"ERROR episode {ep.episode_id} window ends at block {b_end} beyond the cutoff {cut['block']} (8.4)")
        windows[ep.episode_id] = (b0, b_end)
    cache.save(ctx.out / f"header_cache_{ctx.chain}.json")
    draw_ids = draw_five(ctx.episodes)
    (ctx.out / "stage_b_draw.json").write_text(json.dumps({"seed": SEED, "episode_ids": draw_ids}), encoding="utf-8")
    if ctx.run_r1 and ctx.chain in R1_CHAINS:
        max_b0 = max(b0 for b0, _ in windows.values())
        for pool in ctx.pools:
            pull_lifetime(ctx, pool, ctx.lifetime_from.get(pool.key, creation[pool.key]), max_b0)
    rf = ctx.out / f"results_{ctx.chain}.jsonl"
    records = load_records(rf)
    done = {(r["pool_key"], r["episode_id"]) for r in records}
    with open(rf, "a", encoding="utf-8") as fh:
        for ep in sorted(ctx.episodes, key=lambda e: e.t1_first):
            b0, b_end = windows[ep.episode_id]
            for pool in ctx.pools:
                if (pool.key, ep.episode_id) in done:
                    continue
                rec = pool_episode(ctx, pool, ep, b0, b_end, creation[pool.key], cache)
                fh.write(json.dumps(rec, default=str) + "\n")
                fh.flush()
                records.append(rec)
                log(f"{pool.key} ep{ep.episode_id}: {rec['status']} T_rec={rec['t_rec']} censored={rec['censored']} dip={rec['dip']} "
                    f"rereads={rec['band_reread_count']} checked/unchecked={rec['r2_swaps_checked']}/{rec['r2_swaps_unchecked']}")
                Stats.gate()
    cache.save(ctx.out / f"header_cache_{ctx.chain}.json")
    checks = phase_checks(ctx, records, windows, cut["block"], draw_ids)
    return write_pull_record(ctx, cut, records, checks, t_start)


def episodes_from_decision(decision: dict) -> Tuple[List[Episode], Dict[int, Dict[str, bool]]]:
    chosen = decision["decision"]["chosen"]
    s = decision["sets"][chosen]
    eps = [Episode(**e) for e in s["episodes"]]
    flags = {int(k): v for k, v in s["local_flags"].items()}
    return eps, flags


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Depth pull for one chain (prereg 6.1-6.6). Requires the price-only decision and GO.")
    ap.add_argument("--chain", required=True, choices=list(ROUTES))
    ap.add_argument("--decision", default=str(DECISION_DEFAULT))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--eth-timestamps", choices=["log_field", "headers"], default="log_field")
    ap.add_argument("--routes", default=str(ROUTES_OVERRIDE_FILE), help="operational endpoint overrides (JSON), recorded in RUNLOG")
    ap.add_argument("--go", action="store_true", help="confirms the operator's GO for 2024-2026 data")
    args = ap.parse_args(argv)
    if not args.go:
        raise SystemExit("REFUSED: the depth pull needs the operator's GO (--go). No 2024-2026 data was requested.")
    loadguard.apply("depth_pull")
    Stats.gate(force=True)
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    eps, flags = episodes_from_decision(decision)
    live = build_live(load_routes(Path(args.routes))[args.chain])
    if args.chain == "ethereum" and args.eth_timestamps != "log_field":
        live.route = with_timestamps(live.route, args.eth_timestamps)
    ctx = Context(Path(args.out), args.chain, live, eps, flags, lo_guess=LO_GUESS.get(args.chain, 0))
    if args.chain == "ethereum" and STAGE_A_BOUNDS.is_file():
        b = json.loads(STAGE_A_BOUNDS.read_text(encoding="utf-8"))
        for pool in ctx.pools:
            ctx.lifetime_seed_dirs[pool.key] = [STAGE_A_LOGS / pool_dir_name(pool) / "mintburn"]
            ctx.lifetime_from[pool.key] = b["last_2023_block"] + 1
    run(ctx)


if __name__ == "__main__":
    main()
