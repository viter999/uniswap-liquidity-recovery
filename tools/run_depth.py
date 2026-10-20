"""Logged wrapper around the frozen depth-pull driver (operator ruling 17 Oct 2026: option 1, no frozen-code change).

Builds the same Context as pipeline.depth_pull.main() and adds Context.creation_override from
data/confirmatory/creation_override.json (creation blocks established by the factory's PoolCreated events and a getCode
bisection above the Nitro genesis, probes/probe_tenderly_arb.py). Prints the sha256 of this wrapper and of the override
files so RUNLOG can cite them. Everything else is the frozen pipeline.depth_pull.run.
Usage: python -I tools/run_depth.py --chain arbitrum --go [--decision ...] [--out ...] [--eth-timestamps ...] [--routes ...]
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
from pipeline.chains import ROUTES, ROUTES_OVERRIDE_FILE, build_live, load_routes, with_timestamps  # noqa: E402
from pipeline.transport import Stats, log  # noqa: E402
from pipeline.window_pull import pool_dir_name  # noqa: E402

CREATION_FILE = ROOT / "data" / "confirmatory" / "creation_override.json"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_creation(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    return {k: int(v) for k, v in d["creation_blocks"].items() if v is not None}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Depth pull for one chain through the frozen driver, with logged creation overrides.")
    ap.add_argument("--chain", required=True, choices=list(ROUTES))
    ap.add_argument("--decision", default=str(depth_pull.DECISION_DEFAULT))
    ap.add_argument("--out", default=str(depth_pull.OUT_DEFAULT))
    ap.add_argument("--eth-timestamps", choices=["log_field", "headers"], default="log_field")
    ap.add_argument("--routes", default=str(ROUTES_OVERRIDE_FILE))
    ap.add_argument("--creation", default=str(CREATION_FILE))
    ap.add_argument("--go", action="store_true", help="confirms the operator's GO for 2024-2026 data")
    args = ap.parse_args(argv)
    if not args.go:
        raise SystemExit("REFUSED: the depth pull needs the operator's GO (--go). No 2024-2026 data was requested.")
    loadguard.apply("run_depth")
    Stats.gate(force=True)
    log(f"wrapper tools/run_depth.py sha256 {sha256(Path(__file__))}; routes override {args.routes} sha256 "
        f"{sha256(Path(args.routes)) if Path(args.routes).is_file() else 'absent'}; creation override {args.creation} sha256 "
        f"{sha256(Path(args.creation)) if Path(args.creation).is_file() else 'absent'}")
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    eps, flags = depth_pull.episodes_from_decision(decision)
    live = build_live(load_routes(Path(args.routes))[args.chain])
    if args.chain == "ethereum" and args.eth_timestamps != "log_field":
        live.route = with_timestamps(live.route, args.eth_timestamps)
    ctx = depth_pull.Context(Path(args.out), args.chain, live, eps, flags, lo_guess=depth_pull.LO_GUESS.get(args.chain, 0))
    if args.chain == "ethereum" and depth_pull.STAGE_A_BOUNDS.is_file():
        b = json.loads(depth_pull.STAGE_A_BOUNDS.read_text(encoding="utf-8"))
        for pool in ctx.pools:
            ctx.lifetime_seed_dirs[pool.key] = [depth_pull.STAGE_A_LOGS / pool_dir_name(pool) / "mintburn"]
            ctx.lifetime_from[pool.key] = b["last_2023_block"] + 1
    if Path(args.creation).is_file():
        ctx.creation_override = {k: v for k, v in load_creation(Path(args.creation)).items() if k.startswith(args.chain + ":")}
        log(f"creation overrides for {args.chain}: {ctx.creation_override}")
        lf = json.loads(Path(args.creation).read_text(encoding="utf-8")).get("lifetime_from", {})
        for k, v in lf.items():                      # operational: continue the lifetime Mint/Burn pull from this block (chunk cap changed)
            if k.startswith(args.chain + ":"):
                ctx.lifetime_from[k] = int(v)
        if ctx.lifetime_from:
            log(f"lifetime_from overrides for {args.chain}: {ctx.lifetime_from}")
    depth_pull.run(ctx)


if __name__ == "__main__":
    main()
