"""Stage A — calibration checks on 2023 Ethereum data (frozen prereg v0.3 section 6.1), run alongside other workloads.

Operator limits (2026-10-17): below-normal priority; own memory cap 500 MB with data streamed to disk in chunks; free-RAM gate
(pause below 3.3 GiB, resume at 3.5 GiB, checked every minute); writes only under the project folder; public keyless RPC only,
throttled; no 2024-2026 data.

Phases (each resumable through marker/chunk files under data/stageA/):
  bounds    first/last 2023 block via header binary search
  init      Initialize events of both Ethereum pools (R1 needs them)
  mintburn  lifetime Mint/Burn of both pools, factory block .. last 2023 block, 10k-block windows -> CSV chunks
  swaps     all 2023 Swap events of both pools -> CSV chunks
  headers   timestamps of every log-bearing 2023 block (batched eth_getBlockByNumber, 100 per request)
  detect    five calibration windows = the five largest-|dlog60| episodes of 2023 on the clock pool (section 5.2 rules)
  check1    R1 full replay per pool; every 2023 Swap's liquidity field must equal the reconstructed active liquidity;
            the R1 depth series is sampled per minute inside the five windows during the same pass
  check2    R2 snapshot (archive band read at b0) + window replay per pool-window; depth series must agree with R1 within 0.1%
  report    STAGE_A_REPORT.md with sizes, calls, pauses, peak memory, pass/fail

The sampling convention (sampling.py), the band read (band_read.py) and the transport layer (transport.py) are shared
with the confirmatory drivers (D11).
"""
from __future__ import annotations

import csv
import glob
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import loadguard  # noqa: E402
from pipeline.band_read import band_read  # noqa: E402
from pipeline.common import BURN_TOPIC, MINT_TOPIC, SWAP_TOPIC, POOLS, PoolSpec, parse_int24, price_usd_per_eth  # noqa: E402
from pipeline.episodes import PRIMARY, chain_triggers, forward_fill, trigger_minutes  # noqa: E402
from pipeline.pull_logs import RpcError, decode_log, dedupe, iter_ranges  # noqa: E402
from pipeline.reconstruct import Event, PoolState, ReconstructionError, replay_window, state_from_snapshot  # noqa: E402
from pipeline.sampling import Clock, WindowSampler  # noqa: E402
from pipeline.transport import GatedTransport, Stats, get_logs_split, log, retry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "stageA"
LOGS_RPC = "https://rpc.mevblocker.io"          # archive logs, 10k-block windows (probed 2026-10-17)
HDR_RPC = "https://ethereum-rpc.publicnode.com"  # batched headers (probed; bounds only)
CALL_RPC = "https://rpc.mevblocker.io"           # archive eth_call at 2023 blocks, 100-item batches (D5)
LOG_CAP = 10_000
HDR_BATCH = 100
CALL_BATCH = 100
THROTTLE_S = 0.3
FACTORY_BLOCK = 12_369_621
T2023 = 1_672_531_200   # 2023-01-01 00:00 UTC
T2024 = 1_704_067_200   # 2024-01-01 00:00 UTC
INITIALIZE_TOPIC = "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95"
TOL = 0.001
POOL_KEYS = ("ethereum:0.05%", "ethereum:0.3%")
CSV_COLS = ["block", "log_index", "kind", "tick_lower", "tick_upper", "amount", "sqrt_price_x96", "liquidity", "tick"]

LOGS = GatedTransport(LOGS_RPC, timeout_s=90, pause_s=THROTTLE_S)
HDRS = GatedTransport(HDR_RPC, timeout_s=60, pause_s=0.2)
CALLS = GatedTransport(CALL_RPC, timeout_s=60, pause_s=THROTTLE_S)
# header streams (D4): two mevblocker streams pulling parts from a shared queue (publicnode rate-limited a second stream)
HDR_STREAMS = [GatedTransport(LOGS_RPC, timeout_s=90, pause_s=0.2), GatedTransport(LOGS_RPC, timeout_s=90, pause_s=0.2)]


# ----------------------------------------------------------------------------- phase: bounds
def header(n: int, tr=HDRS) -> Tuple[int, str]:
    blk = retry(lambda: tr.call("eth_getBlockByNumber", [hex(n), False]), f"header {n}")
    return int(blk["timestamp"], 16), blk["hash"]


def first_block_at_or_after(ts: int, lo: int, hi: int) -> int:
    while lo < hi:
        mid = (lo + hi) // 2
        if header(mid)[0] >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo


def phase_bounds() -> dict:
    f = OUT / "bounds.json"
    if f.exists():
        return json.loads(f.read_text())
    log("PHASE bounds")
    start = first_block_at_or_after(T2023, 16_000_000, 16_600_000)
    end = first_block_at_or_after(T2024, 18_600_000, 19_200_000) - 1
    b = {"first_2023_block": start, "last_2023_block": end, "ts_first": header(start)[0], "ts_last": header(end)[0]}
    assert b["ts_first"] >= T2023 and b["ts_last"] < T2024
    f.write_text(json.dumps(b, indent=2))
    log(f"bounds {b}")
    return b


# ----------------------------------------------------------------------------- phase: logs -> CSV chunks
def _row(ev: Event) -> list:
    return [ev.block, ev.log_index, ev.kind, ev.tick_lower, ev.tick_upper, ev.amount, ev.sqrt_price_x96, ev.liquidity, ev.tick]


def pull_chunks(pool: PoolSpec, kind: str, topics: list, from_block: int, to_block: int) -> int:
    """One CSV per 10k-block window under data/stageA/logs/<pool>/<kind>/; existing chunks are skipped (resumable)."""
    d = OUT / "logs" / pool.key.replace(":", "_").replace("%", "") / kind
    d.mkdir(parents=True, exist_ok=True)
    n_total = 0
    for a, b in iter_ranges(from_block, to_block, LOG_CAP):
        f = d / f"{a:09d}-{b:09d}.csv"
        if f.exists():
            continue
        raw = retry(lambda: get_logs_split(LOGS, pool.address, topics, a, b), f"getLogs {pool.key} {kind} {a}-{b}")
        rows = [_row(decode_log(lg)) for lg in dedupe(raw)]
        tmp = f.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLS)
            w.writerows(rows)
        tmp.replace(f)
        n_total += len(rows)
    return n_total


def read_chunks(pool: PoolSpec, kind: str) -> Iterator[Event]:
    d = OUT / "logs" / pool.key.replace(":", "_").replace("%", "") / kind
    for f in sorted(glob.glob(str(d / "*.csv"))):
        with open(f, newline="", encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                yield Event(int(row["block"]), int(row["log_index"]), row["kind"], int(row["tick_lower"]), int(row["tick_upper"]),
                            int(row["amount"]), int(row["sqrt_price_x96"]), int(row["liquidity"]), int(row["tick"]))


def merged_events(pool: PoolSpec) -> Iterator[Event]:
    """Initialize, then Mint/Burn and Swap streams merged by (block, log_index)."""
    init = json.loads((OUT / "init.json").read_text())[pool.key]
    yield Event(init["block"], init["log_index"], "initialize", sqrt_price_x96=int(init["sqrt_price_x96"]), tick=init["tick"])
    a, b = read_chunks(pool, "mintburn"), read_chunks(pool, "swap")
    ea, eb = next(a, None), next(b, None)
    while ea is not None or eb is not None:
        if eb is None or (ea is not None and ea.order <= eb.order):
            yield ea
            ea = next(a, None)
        else:
            yield eb
            eb = next(b, None)


def phase_init() -> None:
    f = OUT / "init.json"
    if f.exists():
        return
    log("PHASE init")
    out = {}
    for key in POOL_KEYS:
        pool = POOLS[key]
        flt = {"address": pool.address, "fromBlock": hex(FACTORY_BLOCK), "toBlock": hex(FACTORY_BLOCK + 20_000), "topics": [INITIALIZE_TOPIC]}
        raw = retry(lambda: LOGS.call("eth_getLogs", [flt]), f"Initialize {key}")
        if len(raw) != 1:
            raise SystemExit(f"ERROR expected exactly one Initialize event for {key}, got {len(raw)}")
        lg = raw[0]
        w = lg["data"][2:]
        out[key] = {"block": int(lg["blockNumber"], 16), "log_index": int(lg["logIndex"], 16),
                    "sqrt_price_x96": str(int(w[:64], 16)), "tick": parse_int24(w[64:128])}
        log(f"init {key}: block {out[key]['block']} tick {out[key]['tick']}")
    f.write_text(json.dumps(out, indent=2))


def phase_logs(bounds: dict) -> dict:
    counts = {}
    for key in POOL_KEYS:
        pool = POOLS[key]
        log(f"PHASE mintburn {key}")
        counts[f"{key} mintburn new rows"] = pull_chunks(pool, "mintburn", [[MINT_TOPIC, BURN_TOPIC]], FACTORY_BLOCK, bounds["last_2023_block"])
        log(f"PHASE swaps {key}")
        counts[f"{key} swap new rows"] = pull_chunks(pool, "swap", [SWAP_TOPIC], bounds["first_2023_block"], bounds["last_2023_block"])
    return counts


# ----------------------------------------------------------------------------- phase: headers
def phase_headers(bounds: dict) -> Tuple[np.ndarray, np.ndarray]:
    """Timestamps for every log-bearing block in 2023 (both pools, all kinds) -> headers.npz (blocks, timestamps)."""
    f = OUT / "headers.npz"
    if f.exists():
        z = np.load(f)
        return z["blocks"], z["timestamps"]
    log("PHASE headers")
    blocks = set()
    for key in POOL_KEYS:
        for kind in ("mintburn", "swap"):
            for ev in read_chunks(POOLS[key], kind):
                if ev.block >= bounds["first_2023_block"]:
                    blocks.add(ev.block)
    blocks = np.array(sorted(blocks), dtype=np.int64)
    log(f"headers needed: {len(blocks)} blocks -> {math.ceil(len(blocks) / HDR_BATCH)} batched requests")
    part_dir = OUT / "headers"
    part_dir.mkdir(exist_ok=True)
    ts = np.zeros(len(blocks), dtype=np.int64)
    part = 10_000
    todo = [p0 for p0 in range(0, len(blocks), part) if not (part_dir / f"part_{p0 // part:05d}.npy").exists()]
    for p0 in range(0, len(blocks), part):
        pf = part_dir / f"part_{p0 // part:05d}.npy"
        if pf.exists():
            ts[p0:p0 + part] = np.load(pf)
    log(f"headers: {len(todo)} parts of {part} blocks to fetch, {len(HDR_STREAMS)} streams")
    queue = list(todo)
    q_lock = threading.Lock()
    done = [0]

    def fetch_part(p0: int, tr: GatedTransport) -> np.ndarray:
        chunk = blocks[p0:p0 + part]
        got = np.zeros(len(chunk), dtype=np.int64)
        for i in range(0, len(chunk), HDR_BATCH):
            nums = chunk[i:i + HDR_BATCH]

            def one():
                res = tr.batch([("eth_getBlockByNumber", [hex(int(n)), False]) for n in nums])
                for j, blk in enumerate(res):
                    if blk is None or int(blk["number"], 16) != int(nums[j]):
                        raise RpcError(f"header missing/mismatch at block {int(nums[j])}")
                    got[i + j] = int(blk["timestamp"], 16)
            retry(one, f"headers {int(nums[0])} via {tr.url}")
        return got

    def worker(tr: GatedTransport, idx: int) -> None:
        while True:
            with q_lock:
                if not queue:
                    return
                p0 = queue.pop(0)
            try:
                got = fetch_part(p0, tr)
            except (SystemExit, RpcError) as e:   # this stream gave up: hand the part back and retire the stream
                with q_lock:
                    queue.insert(0, p0)
                log(f"STREAM DOWN {idx} ({tr.url.split('//')[1]}): {str(e)[:120]}")
                return
            np.save(part_dir / f"part_{p0 // part:05d}.npy", got)
            with q_lock:
                ts[p0:p0 + part] = got
                done[0] += 1
                if done[0] % 10 == 0 or done[0] == len(todo):
                    log(f"headers {done[0]}/{len(todo)} parts this run (stream {idx})")

    with ThreadPoolExecutor(max_workers=len(HDR_STREAMS)) as ex:
        futures = [ex.submit(worker, tr, i) for i, tr in enumerate(HDR_STREAMS)]
        for fu in futures:
            fu.result()
    if queue:
        raise SystemExit(f"ERROR header phase incomplete: {len(queue)} parts left after all streams retired")
    assert np.all(np.diff(ts) >= 0), "timestamps not monotone in block order"
    np.savez(f, blocks=blocks, timestamps=ts)
    return blocks, ts


# ----------------------------------------------------------------------------- phase: detect
def phase_detect(clock: Clock, bounds: dict) -> List[dict]:
    f = OUT / "episodes_calibration.json"
    if f.exists():
        return json.loads(f.read_text())
    log("PHASE detect")
    pool = POOLS["ethereum:0.05%"]
    obs: Dict[int, Tuple[Tuple[int, int], float]] = {}
    for ev in read_chunks(pool, "swap"):
        m = clock.minute(ev.block)
        if m not in obs or ev.order > obs[m][0]:
            obs[m] = (ev.order, price_usd_per_eth(ev.sqrt_price_x96, pool))
    m0, m1 = T2023 // 60, T2024 // 60
    prices = forward_fill({m: v for m, (_, v) in obs.items()}, m0, m1)
    trigs = trigger_minutes(m0, prices, PRIMARY)
    eps = chain_triggers(trigs, PRIMARY)
    eps.sort(key=lambda e: -e.max_abs_dlog)
    chosen, skipped = [], []
    for e in eps:
        w_start, w_end = e.window_start, e.window_end
        if w_end >= m1 or w_start < m0:           # window must lie entirely inside 2023 (operator: no 2024 data)
            skipped.append({"t1_first": e.t1_first, "max_abs_dlog": e.max_abs_dlog, "reason": "window crosses 2023 boundary"})
            continue
        chosen.append(e)
        if len(chosen) == 5:
            break
    out = [dict(asdict(e), window_start=e.window_start, window_end=e.window_end,
                t1_first_utc=datetime.fromtimestamp(e.t1_first * 60, tz=timezone.utc).isoformat()) for e in chosen]
    f.write_text(json.dumps({"windows": out, "skipped": skipped, "n_episodes_2023": len(eps), "n_trigger_minutes": len(trigs)}, indent=2))
    for e in out:
        log(f"window {e['t1_first_utc']} max|dlog60|={e['max_abs_dlog']:.4f} span={e['t1_last'] - e['t1_first']}min")
    if skipped:
        log(f"DEVIATION-NOTE {len(skipped)} episode(s) skipped because the window would cross into 2024: {skipped}")
    return json.loads(f.read_text())


# ----------------------------------------------------------------------------- phase: check1 (R1)
def phase_check1(windows: List[dict], clock: Clock, bounds: dict) -> dict:
    f = OUT / "check1.json"
    if f.exists():
        return json.loads(f.read_text())
    results = {}
    for key in POOL_KEYS:
        pool = POOLS[key]
        log(f"PHASE check1 {key}")
        samplers = [WindowSampler(w["window_start"], w["window_end"], clock) for w in windows]
        state: Optional[PoolState] = None
        n_swaps = n_mismatch = n_events = 0
        first_mismatches = []
        cur_block = None
        for ev in merged_events(pool):
            Stats.gate()
            if ev.kind == "initialize":
                state = PoolState(pool, ev.sqrt_price_x96, ev.tick, 0)
                continue
            if ev.block != cur_block:
                if ev.block >= bounds["first_2023_block"]:
                    for s in samplers:
                        s.before_block(ev.block, state)
                cur_block = ev.block
            n_events += 1
            if ev.kind == "swap":
                n_swaps += 1
                try:
                    state.apply(ev)
                except ReconstructionError as e:
                    n_mismatch += 1
                    if len(first_mismatches) < 10:
                        first_mismatches.append(str(e)[:160])
                    state.liquidity = ev.liquidity   # continue with the event's value to keep gathering diagnostics
                    state.sqrt_price_x96, state.tick = ev.sqrt_price_x96, ev.tick
            else:
                state.apply(ev)
            if n_events % 200_000 == 0:
                log(f"check1 {key}: {n_events} events, {n_swaps} swaps, {n_mismatch} mismatches, rss {Stats.rss_mb():.0f}MB")
        for s in samplers:
            s.finish(state)
        series = {str(i): {str(m): v for m, v in s.series.items()} for i, s in enumerate(samplers)}
        (OUT / f"r1_series_{key.replace(':', '_').replace('%', '')}.json").write_text(json.dumps(series))
        results[key] = {"events": n_events, "swaps_2023": n_swaps, "liquidity_mismatches": n_mismatch,
                        "first_mismatches": first_mismatches, "pass": n_mismatch == 0, "initialised_ticks_end": len(state.liquidity_net),
                        "swaps_checked": state.swaps_checked, "swaps_unchecked": state.swaps_unchecked}
        log(f"check1 {key}: {'PASS' if n_mismatch == 0 else 'FAIL'} ({n_swaps} swaps, {n_mismatch} mismatches)")
    f.write_text(json.dumps(results, indent=2))
    return results


# ----------------------------------------------------------------------------- phase: check2 (R2)
def _band_read(pool: PoolSpec, block: int):
    Stats.band_reads += 1
    return band_read(CALLS, pool, block, CALL_BATCH, retry)


def phase_check2(windows: List[dict], clock: Clock) -> dict:
    f = OUT / "check2.json"
    if f.exists():
        return json.loads(f.read_text())
    results = {}
    for key in POOL_KEYS:
        pool = POOLS[key]
        r1 = json.loads((OUT / f"r1_series_{key.replace(':', '_').replace('%', '')}.json").read_text())
        log(f"PHASE check2 {key}")
        per_window = []
        for wi, w in enumerate(windows):
            b0 = clock.last_block_before_minute(w["window_start"])
            snap = _band_read(pool, b0)
            state = state_from_snapshot(pool, snap)
            sampler = WindowSampler(w["window_start"], w["window_end"], clock, b0)
            # last log-bearing block whose minute <= window_end; chunks are block-ordered so reading stops there (memory cap)
            b_end = int(clock.blocks[np.searchsorted(clock.ts, 60 * (w["window_end"] + 1), side="left") - 1])
            evs = []
            for kind in ("mintburn", "swap"):
                for ev in read_chunks(pool, kind):
                    if ev.block > b_end:
                        break
                    if ev.block > b0:
                        evs.append(ev)
            evs.sort(key=lambda e: e.order)
            # replay block by block with the sampler run BEFORE each block (same convention as R1)
            i = 0
            error = None
            try:
                while i < len(evs):
                    blk = evs[i].block
                    sampler.before_block(blk, state)
                    j = i
                    while j < len(evs) and evs[j].block == blk:
                        j += 1
                    replay_window(state, evs[i:j], reader=lambda b, _p=pool: _band_read(_p, b))
                    if state.band_read_failed:
                        break
                    i = j
                if not state.band_read_failed:
                    sampler.finish(state)
            except ReconstructionError as e:     # a failed window is recorded, the other windows still run
                error = str(e)[:200]
                log(f"check2 {key} w{wi}: FAIL {error}")
            s1 = r1[str(wi)]
            max_rel = [0.0, 0.0, 0.0]
            n_cmp = n_none = 0
            for m, d2 in sampler.series.items():
                d1 = s1.get(str(m))
                if d1 is None:
                    continue
                n_cmp += 1
                for zi in range(3):
                    if d1[zi] is None or d2[zi] is None:
                        n_none += 1
                    elif d1[zi] > 0:
                        max_rel[zi] = max(max_rel[zi], abs(d2[zi] - d1[zi]) / d1[zi])
            ok = error is None and (not state.band_read_failed) and all(r <= TOL for r in max_rel) and n_cmp > 0 and n_none == 0
            per_window.append({"window": wi, "b0": b0, "minutes_compared": n_cmp, "max_rel_diff_by_Z": max_rel,
                               "missing_secondary_samples": n_none, "band_rereads": state.band_reread_count,
                               "rereads": state.rereads, "band_read_failed": state.band_read_failed,
                               "swaps_checked": state.swaps_checked, "swaps_unchecked": state.swaps_unchecked,
                               "error": error, "pass": ok})
            log(f"check2 {key} w{wi}: {'PASS' if ok else 'FAIL'} max rel diff {['%.2e' % r for r in max_rel]} rereads {state.band_reread_count} "
                f"({[x['reason'] for x in state.rereads]}) checked/unchecked {state.swaps_checked}/{state.swaps_unchecked}")
        results[key] = {"windows": per_window, "pass": all(p["pass"] for p in per_window)}
    f.write_text(json.dumps(results, indent=2))
    return results


# ----------------------------------------------------------------------------- report
def dir_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2 ** 20


def phase_report(bounds, counts, windows, c1, c2, t_start) -> None:
    passed = all(v["pass"] for v in c1.values()) and all(v["pass"] for v in c2.values())
    lines = [f"# STAGE A REPORT — {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC", "",
             f"Result: **{'PASS' if passed else 'FAIL'}** (check 1: {[v['pass'] for v in c1.values()]}, check 2: {[v['pass'] for v in c2.values()]})", "",
             f"- 2023 blocks {bounds['first_2023_block']}–{bounds['last_2023_block']}; data on disk {dir_size_mb(OUT):.0f} MB under data/stageA/",
             f"- RPC calls this run: {Stats.calls} (window splits for the 10k-result cap: {Stats.splits}; band reads {Stats.band_reads}); bytes received ≈ {Stats.bytes_in / 2 ** 20:.0f} MB; peak own RSS {Stats.peak_rss_mb:.0f} MB; pauses: {Stats.pauses or 'none'}",
             f"- Elapsed {int(time.time() - t_start)} s", "", "## Check 1 (Swap liquidity equality, R1 full replay)", "```", json.dumps(c1, indent=1), "```",
             "## Check 2 (R1 vs R2 depth series on the five calibration windows, tolerance 0.1%)", "```", json.dumps(c2, indent=1), "```",
             "## Calibration windows", "```", json.dumps(windows, indent=1), "```", "## Log pull row counts (new this run)", "```", json.dumps(counts, indent=1), "```"]
    (OUT / "STAGE_A_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"DONE {'PASS' if passed else 'FAIL'} calls={Stats.calls} peak_rss={Stats.peak_rss_mb:.0f}MB pauses={len(Stats.pauses)}")


def main() -> None:
    loadguard.set_below_normal_priority()
    OUT.mkdir(parents=True, exist_ok=True)
    Stats.gate(force=True)
    t_start = time.time()
    bounds = phase_bounds()
    phase_init()
    counts = phase_logs(bounds)
    blocks, ts = phase_headers(bounds)
    clock = Clock(blocks, ts)
    det = phase_detect(clock, bounds)
    windows = det["windows"]
    if len(windows) < 5:
        raise SystemExit(f"ERROR only {len(windows)} calibration windows inside 2023")
    c1 = phase_check1(windows, clock, bounds)
    c2 = phase_check2(windows, clock)
    phase_report(bounds, counts, windows, c1, c2, t_start)


if __name__ == "__main__":
    main()
