"""Price-only phase driver (prereg v0.3 section 5.6; deviations D9, D12). Nothing here runs before the operator's GO.

Reads: Swap logs of the clock pool over W plus margins (their `blockTimestamp` field, cross-checked against headers,
D9), headers for block location, the 50-block formula verification on Base/Unichain inside the episode windows, and
two archive `slot0` reads per pool-episode for both parameter sets. Writes under data/confirmatory/price_only/:
cutoffs, bounds, swap chunks, series.npz, episodes_<set>.json, formula_check_<chain>.json, slot0/<pool>.json,
decision.json and the RUNLOG-ready record (sha256 of the price-series and slot0 files, 5.6 step 5).

Resumable phase by phase; the decision is written before any Mint/Burn log or tick snapshot is requested.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import loadguard
from .band_read import SEL_SLOT0
from .blocks import (ArrayIndex, BlockLocator, HeaderCache, cross_check_timestamps, sample_blocks_in_windows,
                     timestamps_from_logs, verify_formula_on, window_block_range)
from .chains import (ROUTES_OVERRIDE_FILE, SEED, SERIES_END_MIN_EXCL, SERIES_START_MIN, W_END_MIN_EXCL, W_START_MIN, Live, build_live,
                     load_routes, with_timestamps)
from .common import CHAIN_TIME, CLOCK_POOL, MINUTE, POOLS, SWAP_TOPIC, PoolSpec, price_usd_per_eth
from .episodes import ALTERNATE, PRIMARY, Episode, confirmatory, detect_episodes
from .price_series import decode_slot0, run_price_only_phase
from .pull_logs import RpcError, cutoff, decode_log, dedupe, fetch_headers, iter_ranges
from .transport import Stats, get_logs_split, log, retry, utc_now

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "confirmatory" / "price_only"
STAGE_A_BOUNDS = ROOT / "data" / "stageA" / "bounds.json"
LO_GUESS = {"ethereum": 18_800_000, "arbitrum": 150_000_000}   # blocks safely before 2023-12-31 (bisection start)
FORMULA_SAMPLE = 50
CHUNK_CHECK_MIN = 50          # log-field cross-check per 10k-block chunk: 1% of its log-bearing blocks, at least this many
SWAP_COLS = ["block", "log_index", "sqrt_price_x96", "timestamp"]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Context:
    out: Path
    live: Dict[str, Live]                       # every chain that will be read (clock chain + slot0 chains)
    series_start_min: int = SERIES_START_MIN
    series_end_min_excl: int = SERIES_END_MIN_EXCL
    w_start_min: int = W_START_MIN
    w_end_min_excl: int = W_END_MIN_EXCL
    pools: List[PoolSpec] = field(default_factory=lambda: [p for p in POOLS.values() if not p.clock])
    lo_guess: Dict[str, int] = field(default_factory=lambda: dict(LO_GUESS))
    clock_pool: PoolSpec = CLOCK_POOL
    seed_stage_a: bool = True                   # seed the Ethereum bisection with stage A's last 2023 block

    @property
    def clock_live(self) -> Live:
        return self.live[self.clock_pool.chain]


# ----------------------------------------------------------------------------- phases
def phase_cutoffs(ctx: Context) -> Dict[str, dict]:
    out = {}
    for chain, live in ctx.live.items():
        f = ctx.out / f"cutoff_{chain}.json"
        if f.exists():
            out[chain] = json.loads(f.read_text(encoding="utf-8"))
            continue
        c = retry(lambda: cutoff(live.headers), f"cutoff {chain}")
        c.update({"chain": chain, "endpoint": live.route.headers_url, "at_utc": utc_now()})
        f.write_text(json.dumps(c, indent=1), encoding="utf-8")
        out[chain] = c
        log(f"cutoff {chain}: block {c['block']}")
    return out


def locator_for(ctx: Context, chain: str, cutoffs: Dict[str, dict], cache: HeaderCache, index=None) -> BlockLocator:
    route = ctx.live[chain].route
    mode = route.timestamps if route.timestamps == "formula" else "headers"
    return BlockLocator(chain, mode, ctx.live[chain].headers, cache, ctx.lo_guess.get(chain, 0), cutoffs[chain]["block"], index)


def phase_bounds(ctx: Context, cutoffs: Dict[str, dict], cache: HeaderCache) -> dict:
    f = ctx.out / "bounds_clock.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    chain = ctx.clock_pool.chain
    if chain == "ethereum" and ctx.seed_stage_a and STAGE_A_BOUNDS.is_file():   # stage A fixed the last 2023 block; seed the bisection
        b = json.loads(STAGE_A_BOUNDS.read_text(encoding="utf-8"))
        cache.add(b["last_2023_block"], b["ts_last"])
    loc = locator_for(ctx, chain, cutoffs, cache)
    first = loc.first_block_at_or_after(MINUTE * ctx.series_start_min)
    last = loc.block_at_minute_end(ctx.series_end_min_excl - 1)
    if last > cutoffs[chain]["block"]:
        raise SystemExit(f"ERROR series end block {last} is beyond the cutoff {cutoffs[chain]['block']}")
    b = {"chain": chain, "first_block": first, "last_block": last, "series_start_min": ctx.series_start_min,
         "series_end_min_excl": ctx.series_end_min_excl, "header_reads": cache.reads}
    f.write_text(json.dumps(b, indent=1), encoding="utf-8")
    log(f"bounds {chain}: blocks {first}-{last} for minutes [{ctx.series_start_min}, {ctx.series_end_min_excl})")
    return b


def _chunk_timestamps(live: Live, ded: List[dict], blocks: List[int], cache: HeaderCache) -> Tuple[Dict[int, int], dict]:
    route = live.route
    if route.timestamps == "log_field":
        ts_map = timestamps_from_logs(ded)
        chk = cross_check_timestamps(live.headers, ts_map, route.header_batch, minimum=CHUNK_CHECK_MIN, cache=cache)
        if not chk["ok"]:
            raise SystemExit(f"TIMESTAMP MISMATCH in the clock series at {chk['mismatches'][:5]} -- stop; rerun with "
                             f"--eth-timestamps headers (6.6 route) and report (ruling 17 Oct 2026)")
        return ts_map, chk
    if route.timestamps == "headers":
        hdr: Dict[int, dict] = {}
        for i in range(0, len(blocks), route.header_batch):
            part = blocks[i:i + route.header_batch]
            hdr.update(retry(lambda p=part: fetch_headers(live.headers, p, route.header_batch), f"headers {part[0]}"))
        ts_map = {n: h["timestamp"] for n, h in hdr.items()}
        cache.add_many(ts_map.items())
        return ts_map, {"sampled": 0, "mismatches": [], "ok": True, "route": "headers"}
    from .common import timestamp_from_block
    return {n: timestamp_from_block(route.chain, n) for n in blocks}, {"sampled": 0, "mismatches": [], "ok": True, "route": "formula"}


def phase_swaps(ctx: Context, bounds: dict, cache: HeaderCache) -> dict:
    """Clock-pool Swap logs over the series bounds -> swaps/<a>-<b>.csv and index npz per chunk (resumable)."""
    live, pool = ctx.clock_live, ctx.clock_pool
    d = ctx.out / "swaps"
    d.mkdir(parents=True, exist_ok=True)
    summary_f = d / "summary.json"
    summary = json.loads(summary_f.read_text(encoding="utf-8")) if summary_f.exists() else \
        {"chunks": 0, "logs_raw": 0, "logs_deduped": 0, "ts_sampled": 0, "ts_mismatches": 0, "timestamps": live.route.timestamps}
    for a, b in iter_ranges(bounds["first_block"], bounds["last_block"], live.route.log_cap):
        f = d / f"{a:010d}-{b:010d}.csv"
        if f.exists():
            continue
        raw = retry(lambda: get_logs_split(live.logs, pool.address, [SWAP_TOPIC], a, b), f"swaps {a}-{b}")
        ded = dedupe(raw)
        blocks = sorted({int(lg["blockNumber"], 16) for lg in ded})
        ts_map, chk = _chunk_timestamps(live, ded, blocks, cache)
        rows = []
        for lg in ded:
            ev = decode_log(lg)
            rows.append([ev.block, ev.log_index, ev.sqrt_price_x96, ts_map[ev.block]])
        tmp = f.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(SWAP_COLS)
            w.writerows(rows)
        np.savez(d / f"{a:010d}-{b:010d}.index.npz", blocks=np.array(blocks, dtype=np.int64),
                 ts=np.array([ts_map[n] for n in blocks], dtype=np.int64))
        tmp.replace(f)
        summary["chunks"] += 1
        summary["logs_raw"] += len(raw)
        summary["logs_deduped"] += len(ded)
        summary["ts_sampled"] += chk.get("sampled", 0)
        summary["ts_mismatches"] += len(chk.get("mismatches", []))
        summary_f.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        if summary["chunks"] % 20 == 0:
            log(f"swaps: {summary['chunks']} chunks, {summary['logs_deduped']} logs, ts checked {summary['ts_sampled']}")
        Stats.gate()
    return summary


def load_clock_index(ctx: Context) -> ArrayIndex:
    bl, ts = [], []
    for f in sorted((ctx.out / "swaps").glob("*.index.npz")):
        z = np.load(f)
        bl.append(z["blocks"])
        ts.append(z["ts"])
    if not bl:
        return ArrayIndex([], [])
    return ArrayIndex(np.concatenate(bl), np.concatenate(ts))


def ffill(prices: np.ndarray) -> np.ndarray:
    """Carry the last observation forward; NaN until the first observation (5.1, N13)."""
    out = prices.copy()
    last = float("nan")
    for i in range(len(out)):
        if np.isnan(out[i]):
            out[i] = last
        else:
            last = out[i]
    return out


def phase_series(ctx: Context) -> Tuple[np.ndarray, str]:
    """Minute series of the clock pool: last swap per minute, forward-filled (5.1) -> series.npz."""
    f = ctx.out / "series.npz"
    if f.exists():
        return np.load(f)["prices"], sha256_file(f)
    n = ctx.series_end_min_excl - ctx.series_start_min
    prices = np.full(n, np.nan)
    pool = ctx.clock_pool
    for chunk in sorted((ctx.out / "swaps").glob("*.csv")):
        with open(chunk, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):            # rows are (block, log_index)-ordered: the last one in a minute wins
                m = int(row["timestamp"]) // MINUTE - ctx.series_start_min
                if 0 <= m < n:
                    prices[m] = price_usd_per_eth(int(row["sqrt_price_x96"]), pool)
    obs = int(np.sum(~np.isnan(prices)))
    prices = ffill(prices)
    np.savez(f, start_minute=np.int64(ctx.series_start_min), prices=prices, observed_minutes=np.int64(obs))
    log(f"series: {n} minutes, {obs} with a swap")
    return prices, sha256_file(f)


def phase_episodes(ctx: Context, prices: np.ndarray) -> Dict[str, List[Episode]]:
    out = {}
    for params in (PRIMARY, ALTERNATE):
        f = ctx.out / f"episodes_{params.name}.json"
        if f.exists():
            out[params.name] = [Episode(**e) for e in json.loads(f.read_text(encoding="utf-8"))]
            continue
        eps = confirmatory(detect_episodes(ctx.series_start_min, prices, params), ctx.w_start_min, ctx.w_end_min_excl)
        f.write_text(json.dumps([asdict(e) for e in eps], indent=1), encoding="utf-8")
        out[params.name] = eps
        log(f"episodes {params.name}: {len(eps)} confirmatory")
    return out


def phase_formula_checks(ctx: Context, episodes: Dict[str, List[Episode]], cutoffs: Dict[str, dict]) -> Dict[str, dict]:
    """5.6 step 3 / 6.6: 50 random blocks per OP-stack chain inside the episode windows (seed); a mismatch switches the
    chain to headers for the rest of this phase and the depth phase (recorded)."""
    out = {}
    for chain in [c for c in ctx.live if c in CHAIN_TIME]:
        f = ctx.out / f"formula_check_{chain}.json"
        if f.exists():
            out[chain] = json.loads(f.read_text(encoding="utf-8"))
        else:
            live = ctx.live[chain]
            loc = BlockLocator(chain, "formula", None, HeaderCache(), 0, cutoffs[chain]["block"])
            windows = [window_block_range(loc, e.window_start, e.window_end) for eps in episodes.values() for e in eps]
            windows = [(max(a, 0), min(b, cutoffs[chain]["block"])) for a, b in windows if max(a, 0) <= min(b, cutoffs[chain]["block"])]
            blocks = sample_blocks_in_windows(windows, FORMULA_SAMPLE, SEED) if windows else []
            if blocks:
                chk = retry(lambda: verify_formula_on(live.headers, chain, blocks, live.route.header_batch), f"formula {chain}")
            else:
                chk = {"chain": chain, "sampled": 0, "mismatches": [], "ok": True, "note": "no episode window inside the chain block range"}
            chk.update({"windows": len(windows), "at_utc": utc_now()})
            f.write_text(json.dumps(chk, indent=1), encoding="utf-8")
            out[chain] = chk
            log(f"formula check {chain}: {'OK' if chk['ok'] else 'MISMATCH -> headers'} on {chk['sampled']} blocks")
        if not out[chain]["ok"]:
            ctx.live[chain].route = with_timestamps(ctx.live[chain].route, "headers")
    return out


class Slot0Reader:
    """p(pool, minute) through B(m) and an archive slot0 read; results cached per pool on disk (slot0/<pool>.json)."""

    def __init__(self, ctx: Context, cutoffs: Dict[str, dict], caches: Dict[str, HeaderCache], clock_index: ArrayIndex):
        self.ctx, self.cutoffs, self.caches = ctx, cutoffs, caches
        self.locators: Dict[str, BlockLocator] = {}
        for chain in ctx.live:
            idx = clock_index if chain == ctx.clock_pool.chain else None
            self.locators[chain] = locator_for(ctx, chain, cutoffs, caches.setdefault(chain, HeaderCache()), idx)
        self.dir = ctx.out / "slot0"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.files: Dict[str, Path] = {}
        self.cache: Dict[str, Dict[str, dict]] = {}
        self.reads = 0

    def _load(self, key: str) -> Dict[str, dict]:
        if key not in self.cache:
            f = self.dir / (key.replace(":", "_").replace("%", "") + ".json")
            self.files[key] = f
            self.cache[key] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return self.cache[key]

    def _save(self, key: str) -> None:
        self.files[key].write_text(json.dumps(self.cache[key], indent=0, sort_keys=True), encoding="utf-8")

    def price_at(self, key: str, minute: int) -> Optional[float]:
        c = self._load(key)
        k = str(minute)
        if k in c:
            return c[k]["p"]
        pool = POOLS[key]
        block = self.locators[pool.chain].block_at_minute_end(minute)
        tr = self.ctx.live[pool.chain].calls
        if block < 0:                           # chain not yet live at that minute (Unichain before Nov 2024)
            c[k] = {"block": block, "p": None, "sqrt_price_x96": None}
            return None
        try:
            res = retry(lambda: tr.call("eth_call", [{"to": pool.address, "data": SEL_SLOT0}, hex(block)]),
                        f"slot0 {key} m{minute}", attempts=4, give_up="raise")
            p = decode_slot0(res, pool) if res and res != "0x" and len(res) >= 66 else None
        except RpcError as e:                   # N12: unreadable -> local_shock = false for this pool-episode, counted
            log(f"slot0 {key} m{minute}: unreadable ({str(e)[:80]})")
            p, res = None, None
        c[k] = {"block": block, "p": p, "sqrt_price_x96": (str(int(res[2:66], 16)) if p is not None else None)}
        self.reads += 1
        if self.reads % 25 == 0:
            self._save(key)
            Stats.gate()
        return p

    def flush(self) -> None:
        for key in list(self.cache):
            self._save(key)


def phase_decision(ctx: Context, episodes: Dict[str, List[Episode]], reader: Slot0Reader) -> dict:
    f = ctx.out / "decision.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    res = run_price_only_phase({}, ctx.series_start_min, ctx.series_end_min_excl, ctx.w_start_min, ctx.w_end_min_excl,
                               ctx.pools, reader.price_at, episodes_by_set=episodes)
    reader.flush()
    f.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    return res


def write_record(ctx: Context, cutoffs, bounds, swaps, series_sha, formula, decision, caches, t_start) -> dict:
    files = {"series.npz": series_sha}
    for p in sorted((ctx.out / "slot0").glob("*.json")) + sorted(ctx.out.glob("episodes_*.json")):
        files[p.name if p.parent == ctx.out else f"slot0/{p.name}"] = sha256_file(p)
    rec = {"phase": "price-only (5.6)", "started_utc": t_start, "finished_utc": utc_now(), "cutoffs": cutoffs,
           "clock_bounds": bounds, "swaps": swaps, "formula_checks": formula,
           "routes": {c: asdict(l.route) for c, l in ctx.live.items()},
           "counts": {name: {"N": s["n"], "N_conf": s["n_conf"]} for name, s in decision["sets"].items()},
           "decision": decision["decision"], "sha256": files, "header_reads": {c: k.reads for c, k in caches.items()},
           "stats": Stats.snapshot()}
    (ctx.out / "price_only_record.json").write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    lines = [f"## {rec['finished_utc']} — PRICE-ONLY PHASE DONE (5.6)", "",
             f"- Clock pool {ctx.clock_pool.key}: blocks {bounds['first_block']}–{bounds['last_block']}, {swaps['logs_deduped']} Swap logs "
             f"({swaps['logs_raw']} before dedupe), timestamps from `{swaps['timestamps']}`; header cross-check {swaps['ts_sampled']} blocks, "
             f"{swaps['ts_mismatches']} mismatches (D9).",
             f"- Cutoffs: " + "; ".join(f"{c} block {v['block']} hash {v['hash']}" for c, v in cutoffs.items()),
             f"- Formula checks: " + "; ".join(f"{c} {'OK' if v['ok'] else 'MISMATCH'} ({v['sampled']} blocks)" for c, v in formula.items()),
             "- Counts: " + "; ".join(f"{n}: N = {v['N']}, N_conf = {v['N_conf']}" for n, v in rec["counts"].items()),
             f"- Decision: primary set = **{decision['decision'].get('chosen')}** ({decision['decision']})",
             "- sha256: " + "; ".join(f"`{k}` {v}" for k, v in files.items()),
             f"- Requests {Stats.calls}, bytes {Stats.bytes_in / 2 ** 20:.0f} MB, retries {Stats.retries}, rate-limit hits {Stats.rate_limit_hits}, "
             f"pauses {len(Stats.pauses)}, peak RSS {Stats.peak_rss_mb:.0f} MB."]
    (ctx.out / "RUNLOG_price_only.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rec


def run(ctx: Context) -> dict:
    ctx.out.mkdir(parents=True, exist_ok=True)
    t_start = utc_now()
    caches: Dict[str, HeaderCache] = {c: HeaderCache.load(ctx.out / f"header_cache_{c}.json") for c in ctx.live}
    cutoffs = phase_cutoffs(ctx)
    bounds = phase_bounds(ctx, cutoffs, caches[ctx.clock_pool.chain])
    swaps = phase_swaps(ctx, bounds, caches[ctx.clock_pool.chain])
    prices, series_sha = phase_series(ctx)
    episodes = phase_episodes(ctx, prices)
    formula = phase_formula_checks(ctx, episodes, cutoffs)
    reader = Slot0Reader(ctx, cutoffs, caches, load_clock_index(ctx))
    decision = phase_decision(ctx, episodes, reader)
    for c, k in caches.items():
        k.save(ctx.out / f"header_cache_{c}.json")
    rec = write_record(ctx, cutoffs, bounds, swaps, series_sha, formula, decision, caches, t_start)
    log(f"DONE price-only: chosen {decision['decision'].get('chosen')}; counts {rec['counts']}")
    return rec


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Price-only phase (prereg 5.6). Requires the operator's GO.")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--eth-timestamps", choices=["log_field", "headers"], default="log_field",
                    help="Ethereum timestamps: blockTimestamp from eth_getLogs (D9, default) or headers (6.6 route)")
    ap.add_argument("--routes", default=str(ROUTES_OVERRIDE_FILE), help="operational endpoint overrides (JSON), recorded in RUNLOG")
    ap.add_argument("--go", action="store_true", help="confirms the operator's GO for 2024-2026 data")
    args = ap.parse_args(argv)
    if not args.go:
        raise SystemExit("REFUSED: the confirmatory pull needs the operator's GO (--go). No 2024-2026 data was requested.")
    loadguard.apply("price_only")
    Stats.gate(force=True)
    live = {c: build_live(r) for c, r in load_routes(Path(args.routes)).items()}
    if args.eth_timestamps != "log_field":
        live["ethereum"].route = with_timestamps(live["ethereum"].route, args.eth_timestamps)
    run(Context(Path(args.out), live))


if __name__ == "__main__":
    main()
