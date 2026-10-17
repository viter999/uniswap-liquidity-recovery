"""Confirmatory analysis runner (prereg v0.3 sections 7.1-7.10): depth-pull records -> the three confirmatory tests
with verdicts, the pre-declared sensitivities without verdicts, per-pool resilience scores and the pair tallies.

Refuses to run unless every chain's stage B checks passed (checks_<chain>.json, 6.1: "no T_rec is computed before
stage B passes" is enforced here for the analysis; the drivers store metrics only as records).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .analysis import INVALID_REASON_ORDER, build_h1_pairs, build_h2_pairs, run_confirmatory, run_sensitivities
from .common import CONFIRMATORY_CHAINS, POOLS
from .depth_pull import DECISION_DEFAULT, OUT_DEFAULT as DEPTH_OUT, episodes_from_decision
from .episodes import Episode
from .flags import incentive_flags
from .resilience_score import PoolEpisodeResult, resilience_score

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "data" / "confirmatory" / "analysis"


def _num(v) -> float:
    return float("nan") if v is None or (isinstance(v, str) and v.lower() == "nan") else float(v)


def result_from_record(rec: dict, metrics: Optional[dict] = None) -> PoolEpisodeResult:
    m = metrics if metrics is not None else rec
    return PoolEpisodeResult(rec["pool_key"], int(rec["episode_id"]), _num(m.get("d_pre")), _num(m.get("d_min")), bool(m.get("dip", False)),
                             int(m.get("t_rec", rec["t_rec"])), bool(m.get("censored", True)),
                             None if m.get("t50") is None else int(m["t50"]),
                             local_shock=bool(rec["local_shock"]), outage=bool(rec["outage"]), chain_degraded=bool(rec["chain_degraded"]),
                             header_gap=bool(rec["header_gap"]), band_reread_count=int(rec["band_reread_count"]),
                             band_read_failed=bool(rec["band_read_failed"]),
                             pool_absent=bool(m.get("pool_absent", rec["pool_absent"])) or bool(rec["pool_absent"]))


def load_results(depth_out: Path, chains: List[str]) -> Tuple[dict, dict, dict]:
    results: Dict[Tuple[str, int], PoolEpisodeResult] = {}
    by_z: Dict[str, Dict[Tuple[str, int], PoolEpisodeResult]] = {"0.005": {}, "0.02": {}}
    checks = {}
    for chain in chains:
        cf = depth_out / f"checks_{chain}.json"
        if not cf.exists():
            raise SystemExit(f"REFUSED: no checks file for {chain} ({cf}); the depth pull has not finished")
        checks[chain] = json.loads(cf.read_text(encoding="utf-8"))
        rf = depth_out / f"results_{chain}.jsonl"
        for line in rf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            key = (rec["pool_key"], int(rec["episode_id"]))
            results[key] = result_from_record(rec)
            for z, m in (rec.get("metrics_by_z") or {}).items():
                if m is not None and z in by_z:
                    by_z[z][key] = result_from_record(rec, m)
    failed = [c for c, v in checks.items() if not v.get("pass")]
    if failed:
        raise SystemExit(f"REFUSED: stage B checks failed on {failed}; fixes are limited to reconstruct.py/depth.py and logged (6.1)")
    return results, by_z, checks


def episode_flags_for(episodes: List[Episode]) -> Dict[int, Dict[str, bool]]:
    out = {}
    for e in episodes:
        merged: Dict[str, bool] = {}
        for chain in CONFIRMATORY_CHAINS:
            for k, v in incentive_flags(e, chain).items():
                merged[k] = merged.get(k, False) or v
        merged["long_cluster"], merged["pre_in_prior_recovery"], merged["truncated_by_next"] = e.long_cluster, e.pre_in_prior_recovery, e.truncated_by_next
        out[e.episode_id] = merged
    return out


def tallies(results, episodes) -> dict:
    out = {}
    for name, pairs in (("H1", build_h1_pairs(results, episodes)), ("H2-arbitrum", build_h2_pairs(results, episodes, "arbitrum")),
                        ("H2-base", build_h2_pairs(results, episodes, "base"))):
        t = {r: 0 for r in INVALID_REASON_ORDER}
        for p in pairs:
            if not p.valid:
                t[p.reason or "pool_absent"] += 1
        out[name] = {"pairs": len(pairs), "valid": sum(1 for p in pairs if p.valid), "invalid_by_reason": t}
    return out


def resilience(results) -> dict:
    out = {}
    for key in POOLS:
        rs = [r for (k, _), r in results.items() if k == key and r.dip and r.local_shock and not r.outage and not r.pool_absent and not r.band_read_failed]
        out[key] = dict(resilience_score([r.t_rec for r in rs]), n=len(rs), censored=sum(1 for r in rs if r.censored))
    return out


def run(decision: dict, depth_out: Path, out: Path, chains: List[str]) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    episodes, _ = episodes_from_decision(decision)
    results, by_z, checks = load_results(depth_out, chains)
    tests = run_confirmatory(results, episodes)
    flags = episode_flags_for(episodes)
    sens = run_sensitivities(results, episodes, flags, results_by_z={z: r for z, r in by_z.items() if r})
    summary = {"chosen_set": decision["decision"]["chosen"], "episodes": len(episodes), "pool_episodes": len(results),
               "tests": {k: asdict(v) for k, v in tests.items()}, "tallies": tallies(results, episodes),
               "resilience_scores": resilience(results), "checks_pass": {c: v["pass"] for c, v in checks.items()}}
    (out / "confirmatory_tests.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    (out / "sensitivities.json").write_text(json.dumps(sens, indent=1, default=str), encoding="utf-8")
    lines = ["# ANALYSIS SUMMARY (one look, 7.10)", "", f"Chosen set: {summary['chosen_set']}; episodes {len(episodes)}; pool-episodes {len(results)}", "",
             "| test | n | p | p_adj | median ratio | 90% CI | verdict |", "|---|---|---|---|---|---|---|"]
    for k, v in tests.items():
        lines.append(f"| {k} | {v.n} | {v.p:.4g} | {v.p_adj:.4g} | {v.median_ratio:.3f} | [{v.ci90[0]:.3f}, {v.ci90[1]:.3f}] | **{v.verdict}** |")
    lines += ["", "Tallies: " + json.dumps(summary["tallies"]), "", "Resilience scores (median T_rec, 90% bootstrap CI): " + json.dumps(summary["resilience_scores"])]
    (out / "ANALYSIS_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="One-look confirmatory analysis (prereg 7).")
    ap.add_argument("--decision", default=str(DECISION_DEFAULT))
    ap.add_argument("--depth-out", default=str(DEPTH_OUT))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--chains", default=",".join(CONFIRMATORY_CHAINS))
    args = ap.parse_args(argv)
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    s = run(decision, Path(args.depth_out), Path(args.out), args.chains.split(","))
    for k, v in s["tests"].items():
        print(f"{k}: n={v['n']} p={v['p']:.4g} p_adj={v['p_adj']:.4g} M={v['median_ratio']:.3f} verdict={v['verdict']}")


if __name__ == "__main__":
    main()
