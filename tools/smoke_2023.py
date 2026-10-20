"""Live smoke test of the PRODUCTION drivers on the 2023-11-09 calibration window (operator ruling 17 Oct 2026, item 3).

2023 data only. Runs price_only.run and depth_pull.run (Ethereum, Arbitrum, Base; Unichain has no 2023 data) with the
frozen code and the operational route overrides, then run_analysis.run, and compares the Ethereum depth series with the
stage A R1 series of the same window. Any failure is reported, never fixed here (frozen code).
Outputs under data/smoke_2023/; summary in data/smoke_2023/SMOKE_REPORT.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import depth_pull, loadguard, price_only, run_analysis  # noqa: E402
from pipeline.chains import ROUTES_OVERRIDE_FILE, SERIES_MARGIN_AFTER_MIN, SERIES_MARGIN_BEFORE_MIN, build_live, load_routes  # noqa: E402
from pipeline.common import POOLS  # noqa: E402
from pipeline.episodes import Episode  # noqa: E402
from pipeline.transport import Stats, log, utc_now  # noqa: E402
from pipeline.window_pull import pool_dir_name  # noqa: E402

OUT = ROOT / "data" / "smoke_2023"
STAGE_A = ROOT / "data" / "stageA"
CHAINS = ("ethereum", "arbitrum", "base")
W_START_MIN = 1_699_401_600 // 60       # 2023-11-08 00:00 UTC
W_END_MIN_EXCL = 1_699_660_800 // 60    # 2023-11-11 00:00 UTC
LO_GUESS = {"ethereum": 18_000_000, "arbitrum": 140_000_000}
STAGE_A_WINDOW_INDEX = 4                # 2023-11-09 15:10 UTC first trigger (episodes_calibration.json)
TOL = 1e-9


def main() -> None:
    loadguard.apply("smoke_2023")
    Stats.gate(force=True)
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    report = {"started_utc": utc_now(), "routes_override": str(ROUTES_OVERRIDE_FILE), "steps": {}}
    routes = load_routes()
    report["routes"] = {c: {"logs_url": r.logs_url, "log_cap": r.log_cap, "calls_url": r.calls_url, "call_batch": r.call_batch,
                            "timestamps": r.timestamps} for c, r in routes.items()}
    live = {c: build_live(routes[c]) for c in CHAINS}

    # ---- price-only phase on the smoke window
    pools = [p for p in POOLS.values() if p.chain in CHAINS and not p.clock]
    ctx = price_only.Context(OUT / "price_only", live, W_START_MIN - SERIES_MARGIN_BEFORE_MIN, W_END_MIN_EXCL + SERIES_MARGIN_AFTER_MIN,
                             W_START_MIN, W_END_MIN_EXCL, pools, lo_guess=dict(LO_GUESS))
    rec = price_only.run(ctx)
    eps = [Episode(**e) for e in json.loads((OUT / "price_only" / "episodes_5pct_60min.json").read_text(encoding="utf-8"))]
    cal = json.loads((STAGE_A / "episodes_calibration.json").read_text(encoding="utf-8"))["windows"][STAGE_A_WINDOW_INDEX]
    match = [e for e in eps if e.t1_first == cal["t1_first"]]
    step = {"counts": rec["counts"], "decision": rec["decision"], "episodes_primary": [(e.episode_id, e.t1_first, e.t1_last, e.t0) for e in eps],
            "stage_a_window": (cal["t1_first"], cal["t1_last"], cal["t0"]),
            "episode_matches_stage_a": bool(match) and (match[0].t1_last, match[0].t0) == (cal["t1_last"], cal["t0"]),
            "ts_check": rec["swaps"], "formula_checks": rec["formula_checks"]}
    report["steps"]["price_only"] = step
    log(f"price-only: counts {rec['counts']} match stage A window: {step['episode_matches_stage_a']}")
    if not step["episode_matches_stage_a"]:
        report["pass"] = False
        report["stop_reason"] = "the primary-set episode does not match the stage A calibration window"
        (OUT / "SMOKE_REPORT.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        raise SystemExit("SMOKE STOP: episode mismatch with stage A")
    ep = match[0]
    decision = json.loads((OUT / "price_only" / "decision.json").read_text(encoding="utf-8"))
    flags = {int(k): v for k, v in decision["sets"]["5pct_60min"]["local_flags"].items()}

    # ---- depth pull per chain (the primary-set episode, as the frozen order would after the decision)
    bounds = json.loads((STAGE_A / "bounds.json").read_text(encoding="utf-8"))
    depth_out = OUT / "depth"
    creation_file = ROOT / "data" / "confirmatory" / "creation_override.json"
    creation = {k: int(v) for k, v in json.loads(creation_file.read_text(encoding="utf-8"))["creation_blocks"].items() if v is not None}         if creation_file.is_file() else {}
    report["creation_override"] = creation
    for chain in CHAINS:
        dctx = depth_pull.Context(depth_out, chain, live[chain], [ep], flags, lo_guess=LO_GUESS.get(chain, 0),
                                  creation_override={k: v for k, v in creation.items() if k.startswith(chain + ":")})
        if chain == "ethereum":
            for p in POOLS.values():
                if p.chain == "ethereum":
                    dctx.lifetime_seed_dirs[p.key] = [STAGE_A / "logs" / pool_dir_name(p) / "mintburn"]
                    dctx.lifetime_from[p.key] = bounds["last_2023_block"] + 1
        drec = depth_pull.run(dctx)
        checks = json.loads((depth_out / f"checks_{chain}.json").read_text(encoding="utf-8"))
        recs = depth_pull.load_records(depth_out / f"results_{chain}.jsonl")
        report["steps"][f"depth_{chain}"] = {
            "checks_pass": checks["pass"], "status": {r["pool_key"]: r["status"] for r in recs},
            "t_rec": {r["pool_key"]: (r["t_rec"], r["censored"], r["dip"]) for r in recs},
            "rereads": {r["pool_key"]: [x["reason"] for x in r["rereads"]] for r in recs},
            "counters": {r["pool_key"]: (r["r2_swaps_checked"], r["r2_swaps_unchecked"]) for r in recs},
            "r1": {r["pool_key"]: (r["r1"] or {}).get("mismatches") for r in recs},
            "r1_r2": {r["pool_key"]: (r["r1_r2"] or {}).get("max_rel_diff_by_Z") for r in recs},
            "ts_check": {r["pool_key"]: r.get("ts_check") for r in recs}, "stats": drec["stats"]}
        log(f"depth {chain}: checks {'PASS' if checks['pass'] else 'FAIL'} {report['steps'][f'depth_{chain}']['status']}")

    # ---- compare the Ethereum depth series with the stage A R1 series of the same window
    cmp = {}
    for key in ("ethereum:0.05%", "ethereum:0.3%"):
        s1 = json.loads((STAGE_A / f"r1_series_{pool_dir_name(POOLS[key])}.json").read_text(encoding="utf-8"))[str(STAGE_A_WINDOW_INDEX)]
        sf = depth_out / "series" / pool_dir_name(POOLS[key]) / f"ep{ep.episode_id}.npz"
        if not sf.exists():
            cmp[key] = {"pass": False, "reason": "no smoke series"}
            continue
        z = np.load(sf)
        smoke = {int(m): (a, b, c) for m, a, b, c in zip(z["minute"], z["D_0.005"], z["D_0.01"], z["D_0.02"])}
        max_rel, n = [0.0, 0.0, 0.0], 0
        for m, rec1 in s1.items():
            v = smoke.get(int(m))
            if v is None:
                continue
            n += 1
            for i in range(3):
                if rec1[i] and rec1[i] > 0:
                    max_rel[i] = max(max_rel[i], abs(v[i] - rec1[i]) / rec1[i])
        cmp[key] = {"minutes_compared": n, "max_rel_diff_by_Z": max_rel, "pass": n > 0 and all(r <= TOL for r in max_rel)}
        log(f"stage A comparison {key}: {n} minutes, max rel diff {max_rel} -> {'PASS' if cmp[key]['pass'] else 'FAIL'}")
    report["steps"]["stage_a_comparison"] = cmp

    # ---- analysis runner (one episode: everything Underpowered, but the path runs)
    try:
        summary = run_analysis.run({"decision": {"chosen": "5pct_60min"}, "sets": {"5pct_60min": {"episodes": [e.__dict__ for e in [ep]], "local_flags": {}}}},
                                   depth_out, OUT / "analysis", list(CHAINS))
        report["steps"]["analysis"] = {"ran": True, "verdicts": {k: v["verdict"] for k, v in summary["tests"].items()}}
    except SystemExit as e:
        report["steps"]["analysis"] = {"ran": False, "error": str(e)}

    report["pass"] = all(report["steps"][f"depth_{c}"]["checks_pass"] for c in CHAINS) and all(v["pass"] for v in cmp.values()) \
        and report["steps"]["analysis"]["ran"]
    report["elapsed_s"] = int(time.time() - t0)
    report["finished_utc"] = utc_now()
    (OUT / "SMOKE_REPORT.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    log(f"SMOKE {'PASS' if report['pass'] else 'FAIL'} in {report['elapsed_s']} s")


if __name__ == "__main__":
    main()
