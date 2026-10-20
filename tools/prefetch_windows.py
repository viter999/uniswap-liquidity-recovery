"""Operational pre-fetch of the official-endpoint parts of a chain's depth pull (operator ruling 18 Oct 2026; no frozen-code
change). Calls the frozen functions exactly as pipeline.depth_pull.run does, in the same order, writing the same resumable
cache files: cutoff_<chain>.json, creation_<pool>.json, header_cache_<chain>.json, lifetime/<pool>/mintburn/*.csv and
windows/<pool>/ep<id>/{logs.csv, timestamps.npz, meta.json}. It never calls eth_call (no band read, no slot0), so Tenderly
is untouched; the later tools/run_depth.py run finds every window complete and performs only the eth_call steps and the
replays. Windows are visited latest-episode-first so a driver started later (earliest-first) meets it at most once.
Usage: python tools/prefetch_windows.py --chain arbitrum --go
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import depth_pull, loadguard  # noqa: E402
from pipeline.blocks import BlockLocator, HeaderCache, window_block_range  # noqa: E402
from pipeline.chains import ROUTES, ROUTES_OVERRIDE_FILE, build_live, load_routes  # noqa: E402
from pipeline.flags import outage_flags  # noqa: E402
from pipeline.pull_logs import cutoff  # noqa: E402
from pipeline.pull_logs import RpcError, decode_log, dedupe, iter_ranges  # noqa: E402
from pipeline.transport import Stats, get_logs_split, is_result_cap, log, retry, utc_now  # noqa: E402
from pipeline.window_pull import CSV_COLS, event_row, pool_dir_name, pull_window  # noqa: E402

CREATION_FILE = ROOT / "data" / "confirmatory" / "creation_override.json"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


CAP_PHRASES = ("exceeds limit", "exceed", "more than", "too many", "limit")
FALLBACK_CAP = 10_000


def guarded_lifetime(ctx, pool, from_block: int, to_block: int, cap: int) -> dict:
    """Lifetime Mint/Burn chunks at `cap` blocks; a chunk refused for the endpoint's result cap is redone as FALLBACK_CAP
    sub-chunks (files named by their own ranges, so the coverage stays contiguous). Same CSV layout as the frozen driver."""
    import csv
    d = ctx.out / "lifetime" / pool_dir_name(pool) / "mintburn"
    d.mkdir(parents=True, exist_ok=True)
    redone = []

    def write(a: int, b: int, rows: list) -> None:
        f = d / f"{a:010d}-{b:010d}.csv"
        tmp = f.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLS)
            w.writerows(rows)
        tmp.replace(f)

    def pull(a: int, b: int) -> list:
        raw = retry(lambda: get_logs_split(ctx.live.logs, pool.address, [[depth_pull.MINT_TOPIC, depth_pull.BURN_TOPIC]], a, b),
                    f"lifetime {pool.key} {a}-{b}", attempts=4, give_up="raise")
        return [event_row(decode_log(lg)) for lg in dedupe(raw)]

    for a, b in iter_ranges(from_block, to_block, cap):
        if (d / f"{a:010d}-{b:010d}.csv").exists():
            continue
        if cap > FALLBACK_CAP and all((d / f"{x:010d}-{y:010d}.csv").exists() for x, y in iter_ranges(a, b, FALLBACK_CAP)):
            continue                                   # already covered by earlier sub-chunks
        try:
            write(a, b, pull(a, b))
        except RpcError as e:
            msg = str(e).lower()
            if cap > FALLBACK_CAP and (is_result_cap(msg) or any(k in msg for k in CAP_PHRASES)):
                log(f"CAP GUARD {pool.key} {a}-{b}: {str(e)[:90]} -> redoing at {FALLBACK_CAP}-block sub-chunks")
                for x, y in iter_ranges(a, b, FALLBACK_CAP):
                    if not (d / f"{x:010d}-{y:010d}.csv").exists():
                        write(x, y, retry(lambda x=x, y=y: pull(x, y), f"lifetime {pool.key} {x}-{y} (sub-chunk)"))
                redone.append((a, b))
            else:
                raise SystemExit(f"ERROR lifetime {pool.key} {a}-{b}: {str(e)[:160]}")
        Stats.gate()
    return {"pool": pool.key, "cap": cap, "redone_at_fallback": redone}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Pre-fetch the official-endpoint parts of one chain's depth pull (frozen functions only).")
    ap.add_argument("--chain", required=True, choices=list(ROUTES))
    ap.add_argument("--decision", default=str(depth_pull.DECISION_DEFAULT))
    ap.add_argument("--out", default=str(depth_pull.OUT_DEFAULT))
    ap.add_argument("--routes", default=str(ROUTES_OVERRIDE_FILE))
    ap.add_argument("--creation", default=str(CREATION_FILE))
    ap.add_argument("--pools", default="", help="comma-separated pool keys to restrict this process to (operational parallelism per pool)")
    ap.add_argument("--lifetime-only", action="store_true", help="stop after the lifetime Mint/Burn continuation")
    ap.add_argument("--lifetime-cap", type=int, default=0, help="chunk size for the lifetime pull (0 = the route's log_cap); a chunk that hits the result cap is redone at 10,000 blocks (ruling 18 Oct 2026)")
    ap.add_argument("--episodes", default="", help="comma-separated episode ids to restrict the window pull to (fallback re-pulls)")
    ap.add_argument("--windows-only", action="store_true", help="skip the lifetime pull; pull the windows only")
    ap.add_argument("--go", action="store_true")
    args = ap.parse_args(argv)
    if not args.go:
        raise SystemExit("REFUSED: needs the operator's GO (--go).")
    loadguard.apply("prefetch_windows")
    Stats.gate(force=True)
    log(f"prefetch wrapper sha256 {sha256(Path(__file__))}; routes override sha256 {sha256(Path(args.routes))}; creation override sha256 "
        f"{sha256(Path(args.creation)) if Path(args.creation).is_file() else 'absent'}")
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    eps, flags = depth_pull.episodes_from_decision(decision)
    live = build_live(load_routes(Path(args.routes))[args.chain])
    ctx = depth_pull.Context(Path(args.out), args.chain, live, eps, flags, lo_guess=depth_pull.LO_GUESS.get(args.chain, 0))
    only = [k.strip() for k in args.pools.split(",") if k.strip()]
    pools = [p for p in ctx.pools if not only or p.key in only]
    if Path(args.creation).is_file():
        cj = json.loads(Path(args.creation).read_text(encoding="utf-8"))
        d = cj["creation_blocks"]
        ctx.creation_override = {k: int(v) for k, v in d.items() if v is not None and k.startswith(args.chain + ":")}
        for k, v in cj.get("lifetime_from", {}).items():   # operational: continue the lifetime Mint/Burn pull from this block (chunk cap changed)
            if k.startswith(args.chain + ":"):
                ctx.lifetime_from[k] = int(v)
        log(f"creation overrides {ctx.creation_override}; lifetime_from {ctx.lifetime_from}")
    ctx.out.mkdir(parents=True, exist_ok=True)
    # --- the same steps, in the same order, as the frozen depth_pull.run (everything before the per-pool-episode loop)
    cf = ctx.out / f"cutoff_{ctx.chain}.json"
    if cf.exists():
        cut = json.loads(cf.read_text(encoding="utf-8"))
    else:
        cut = retry(lambda: cutoff(ctx.live.headers), f"cutoff {ctx.chain}")
        cut.update({"chain": ctx.chain, "at_utc": utc_now()})
        cf.write_text(json.dumps(cut, indent=1), encoding="utf-8")
    creation = depth_pull.phase_creation(ctx, cut["block"])
    cache = HeaderCache.load(ctx.out / f"header_cache_{ctx.chain}.json")
    mode = "formula" if ctx.live.route.timestamps == "formula" else "headers"
    locator = BlockLocator(ctx.chain, mode, ctx.live.headers, cache, ctx.lo_guess, cut["block"])
    windows = {}
    for ep in ctx.episodes:
        b0, b_end = window_block_range(locator, ep.window_start, ep.window_end)
        if b_end > cut["block"]:
            raise SystemExit(f"ERROR episode {ep.episode_id} window ends at block {b_end} beyond the cutoff {cut['block']} (8.4)")
        windows[ep.episode_id] = (b0, b_end)
    cache.save(ctx.out / f"header_cache_{ctx.chain}.json")
    (ctx.out / f"prefetch_windows_{ctx.chain}.json").write_text(json.dumps({"cutoff": cut, "windows": windows}, indent=1), encoding="utf-8")
    log(f"windows located for {len(windows)} episodes; cutoff {cut['block']}")
    if ctx.chain in depth_pull.R1_CHAINS and not args.windows_only:
        max_b0 = max(b0 for b0, _ in windows.values())
        for pool in pools:
            if args.lifetime_cap:
                g = guarded_lifetime(ctx, pool, ctx.lifetime_from.get(pool.key, creation[pool.key]), max_b0, args.lifetime_cap)
                log(f"lifetime Mint/Burn complete for {pool.key} up to block {max_b0} at {args.lifetime_cap}-block chunks; redone at fallback: {g['redone_at_fallback']}")
                (ctx.out / f"lifetime_guard_{pool_dir_name(pool)}.json").write_text(json.dumps(g, indent=1), encoding="utf-8")
            else:
                depth_pull.pull_lifetime(ctx, pool, ctx.lifetime_from.get(pool.key, creation[pool.key]), max_b0)
                log(f"lifetime Mint/Burn complete for {pool.key} up to block {max_b0}")
    if args.lifetime_only:
        log(f"LIFETIME DONE {ctx.chain} pools {[p.key for p in pools]} (lifetime-only run)")
        return
    # --- window logs + headers, latest episode first, skipping what the driver would not pull either
    n_done = n_skip = 0
    only_eps = {int(x) for x in args.episodes.split(",") if x.strip()}
    for ep in sorted(ctx.episodes, key=lambda e: -e.t1_first):
        if only_eps and ep.episode_id not in only_eps:
            continue
        b0, b_end = windows[ep.episode_id]
        for pool in pools:
            if outage_flags(ep, ctx.chain)["outage"] or creation[pool.key] > b0:
                n_skip += 1
                continue
            pull_window(ctx.live, pool, ep.episode_id, b0, b_end, ctx.out / "windows", cache)
            n_done += 1
            Stats.gate()
        cache.save(ctx.out / f"header_cache_{ctx.chain}.json")
    cache.save(ctx.out / f"header_cache_{ctx.chain}.json")
    log(f"PREFETCH DONE {ctx.chain} pools {[p.key for p in pools]}: {n_done} windows complete, {n_skip} skipped (outage/absent); stats {Stats.snapshot()}")


if __name__ == "__main__":
    main()
