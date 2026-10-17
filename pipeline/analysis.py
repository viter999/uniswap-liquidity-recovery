"""Pairing, episode statistics, confirmatory tests, verdicts and sensitivities (prereg v0.3 sections 7.1-7.8).

Inputs: {(pool_key, episode_id): PoolEpisodeResult} and the episode list. Outputs: one TestResult per confirmatory test
(H1, H2-Arbitrum, H2-Base) plus sensitivity runs reported without verdicts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .common import CONFIRMATORY_CHAINS
from .episodes import Episode
from .resilience_score import (PoolEpisodeResult, bootstrap_median_ratio, holm, sign_test, stratified_signed_rank,
                               wilcoxon_signed_rank, SEED, BOOT_B, SCIPY_AVAILABLE)

MEI = 1.5
ALPHA = 0.05
N_FLOOR = 20
INVALID_REASON_ORDER = ("pool_absent", "outage", "band_read_failed", "local_shock", "dip", "both_censored")
TIERS = ("0.05%", "0.3%")


@dataclass
class PairObs:
    episode_id: int
    stratum: str
    r: float
    censor_state: str          # "uncensored" | "one" | "both"
    valid: bool
    reason: Optional[str] = None


def pair_r(t_num: int, t_den: int) -> float:
    """log((T_num + 1) / (T_den + 1)) in minutes (6.3)."""
    return math.log((t_num + 1.0) / (t_den + 1.0))


def _validity(a: PoolEpisodeResult, b: PoolEpisodeResult) -> Tuple[bool, Optional[str], str]:
    for reason in INVALID_REASON_ORDER:
        if reason == "both_censored":
            if a.censored and b.censored:
                return False, reason, "both"
        elif reason in ("local_shock", "dip"):
            if not getattr(a, reason) or not getattr(b, reason):
                return False, reason, "n/a"
        else:
            if getattr(a, reason) or getattr(b, reason):
                return False, reason, "n/a"
    state = "one" if (a.censored != b.censored) else "uncensored"
    return True, None, state


def build_h1_pairs(results: Dict[Tuple[str, int], PoolEpisodeResult], episodes: Sequence[Episode],
                   chains: Sequence[str] = CONFIRMATORY_CHAINS) -> List[PairObs]:
    """r1_c = log((T(0.3%, c)+1)/(T(0.05%, c)+1)) per chain (7.2)."""
    out = []
    for e in episodes:
        for c in chains:
            a, b = results.get((f"{c}:0.3%", e.episode_id)), results.get((f"{c}:0.05%", e.episode_id))
            if a is None or b is None:
                out.append(PairObs(e.episode_id, c, float("nan"), "n/a", False, "pool_absent"))
                continue
            valid, reason, state = _validity(a, b)
            out.append(PairObs(e.episode_id, c, pair_r(a.t_rec, b.t_rec), state, valid, reason))
    return out


def build_h2_pairs(results: Dict[Tuple[str, int], PoolEpisodeResult], episodes: Sequence[Episode], l2: str) -> List[PairObs]:
    """r2_tau = log((T(Ethereum, tau)+1)/(T(L2, tau)+1)) per tier (7.3)."""
    out = []
    for e in episodes:
        for tier in TIERS:
            a, b = results.get((f"ethereum:{tier}", e.episode_id)), results.get((f"{l2}:{tier}", e.episode_id))
            if a is None or b is None:
                out.append(PairObs(e.episode_id, tier, float("nan"), "n/a", False, "pool_absent"))
                continue
            valid, reason, state = _validity(a, b)
            out.append(PairObs(e.episode_id, tier, pair_r(a.t_rec, b.t_rec), state, valid, reason))
    return out


# ----------------------------------------------------------------------------- episode statistics (7.4)
@dataclass
class EpisodeStat:
    episode_id: int
    d_val: float
    d_rank: float
    n_valid: int
    n_uncensored: int
    rank_only: bool
    pairs_r: List[float] = field(default_factory=list)


def episode_stats(pairs: Sequence[PairObs]) -> Tuple[List[EpisodeStat], dict]:
    by_e: Dict[int, List[PairObs]] = {}
    tally: Dict[str, int] = {r: 0 for r in INVALID_REASON_ORDER}
    for p in pairs:
        if not p.valid:
            tally[p.reason or "pool_absent"] += 1
            continue
        by_e.setdefault(p.episode_id, []).append(p)
    stats: List[EpisodeStat] = []
    for eid, ps in sorted(by_e.items()):
        rs = [p.r for p in ps]
        unc = [p.r for p in ps if p.censor_state == "uncensored"]
        d_val = float(np.mean(rs))
        if unc:
            stats.append(EpisodeStat(eid, d_val, float(np.mean(unc)), len(ps), len(unc), False, rs))
        else:
            stats.append(EpisodeStat(eid, d_val, float("nan"), len(ps), 0, True, rs))
    # rank-only replacement: sign(d_val) x (max |d_rank| over uncensored episodes + 1), max fixed from the full sample
    unc_abs = [abs(s.d_rank) for s in stats if not s.rank_only]
    max_abs = max(unc_abs) if unc_abs else None
    for s in stats:
        if s.rank_only:
            sgn = (s.d_val > 0) - (s.d_val < 0)
            s.d_rank = float(sgn * (max_abs + 1.0)) if max_abs is not None else float("nan")
    counts = {"invalid_pairs_by_reason": tally, "episodes_with_valid_pair": len(stats),
              "uncensored_episodes": sum(1 for s in stats if not s.rank_only),
              "one_censored_episodes": sum(1 for s in stats if s.rank_only),
              "no_uncensored_episode": max_abs is None and bool(stats)}
    return stats, counts


# ----------------------------------------------------------------------------- tests and verdicts (7.5-7.7)
@dataclass
class TestResult:
    name: str
    n: int
    p: float
    p_adj: float
    median_ratio: float
    ci90: Tuple[float, float]
    verdict: str
    counts: dict
    test: dict
    sign: dict
    zeros_dropped: int


def run_test(name: str, stats: Sequence[EpisodeStat], counts: dict, b: int = BOOT_B, seed: int = SEED) -> TestResult:
    d_rank = [s.d_rank for s in stats if not math.isnan(s.d_rank)]
    zeros = sum(1 for v in d_rank if v == 0.0)
    nonzero = [v for v in d_rank if v != 0.0]
    n = len(nonzero)
    test = wilcoxon_signed_rank(nonzero) if n else {"n": 0, "p": float("nan"), "method": "none", "impl": "none"}
    d_val = [s.d_val for s in stats]
    m = float(np.exp(np.median(d_val))) if d_val else float("nan")
    ci = bootstrap_median_ratio([s.pairs_r for s in stats], b=b, seed=seed) if d_val else (float("nan"), float("nan"))
    sign = sign_test(d_val)
    res = TestResult(name, n, float(test["p"]), float(test["p"]), m, ci, "pending", dict(counts), test, sign, zeros)
    res.verdict = verdict(res, underpowered=(n < N_FLOOR or counts.get("no_uncensored_episode", False)))
    return res


def verdict(res: TestResult, underpowered: bool) -> str:
    if underpowered:
        return "Underpowered"
    if res.p_adj < ALPHA and res.median_ratio >= MEI:
        return "Yes"
    if not math.isnan(res.ci90[1]) and res.ci90[1] < MEI:
        return "No"
    return "Null"


def run_confirmatory(results: Dict[Tuple[str, int], PoolEpisodeResult], episodes: Sequence[Episode],
                     b: int = BOOT_B, seed: int = SEED) -> Dict[str, TestResult]:
    """H1, H2-Arbitrum, H2-Base; Holm across the two H2 p-values (7.7). Refuses to run without scipy (7.5)."""
    if not SCIPY_AVAILABLE:
        raise RuntimeError("prereg 7.5 names scipy.stats.wilcoxon for the confirmatory tests; scipy is not importable")
    out: Dict[str, TestResult] = {}
    s1, c1 = episode_stats(build_h1_pairs(results, episodes))
    out["H1"] = run_test("H1", s1, c1, b, seed)
    h2 = {}
    for l2 in ("arbitrum", "base"):
        s2, c2 = episode_stats(build_h2_pairs(results, episodes, l2))
        h2[l2] = run_test(f"H2-{l2}", s2, c2, b, seed)
    adj = holm([h2["arbitrum"].p, h2["base"].p])
    for (l2, res), pa in zip(h2.items(), adj):
        res.p_adj = pa
        res.verdict = verdict(res, underpowered=(res.n < N_FLOOR or res.counts.get("no_uncensored_episode", False)))
        out[res.name] = res
    return out


# ----------------------------------------------------------------------------- sensitivities (7.8), no verdicts
def _strip(res: TestResult) -> dict:
    return {"n": res.n, "p": res.p, "median_ratio": res.median_ratio, "ci90": res.ci90, "sign": res.sign}


def run_sensitivities(results: Dict[Tuple[str, int], PoolEpisodeResult], episodes: Sequence[Episode],
                      episode_flags: Dict[int, Dict[str, bool]], b: int = BOOT_B, seed: int = SEED,
                      results_by_z: Optional[Dict[str, Dict[Tuple[str, int], PoolEpisodeResult]]] = None) -> dict:
    """S-a, S-b, S-c, S-e, S-f, S-g, S-h and S-j (7.8) reported without verdicts; S-d is inside every TestResult.

    results_by_z: {"0.005": results, "0.02": results} evaluated on the secondary depth bands (S-h)."""
    def h1(eps, res_map, chains=CONFIRMATORY_CHAINS):
        s, c = episode_stats(build_h1_pairs(res_map, eps, chains))
        return _strip(run_test("H1", s, c, b, seed))

    def h2(eps, res_map, l2):
        s, c = episode_stats(build_h2_pairs(res_map, eps, l2))
        return _strip(run_test(f"H2-{l2}", s, c, b, seed))

    def excl(*flags):
        return [e for e in episodes if not any(episode_flags.get(e.episode_id, {}).get(f, False) for f in flags)]

    out = {}
    out["S-a"] = {"H1": h1(excl("incentive_v4_pull", "structural_fee_switch"), results),
                  "H2-arbitrum": h2(excl("incentive_v4_pull", "structural_fee_switch"), results, "arbitrum"),
                  "H2-base": h2(excl("incentive_v4_pull", "structural_fee_switch"), results, "base")}
    # S-b: Arbitrum additionally excludes incentive_possible -> implement by dropping arbitrum pairs of flagged episodes
    res_b = {k: v for k, v in results.items() if not (k[0].startswith("arbitrum") and episode_flags.get(k[1], {}).get("incentive_possible", False))}
    out["S-b"] = {"H1": h1(excl("incentive_v4_pull", "structural_fee_switch"), res_b),
                  "H2-arbitrum": h2(excl("incentive_v4_pull", "structural_fee_switch"), res_b, "arbitrum")}
    # S-c: dip=false pool-episodes enter with T_rec = 0 and pairs retained
    res_c = {}
    for k, v in results.items():
        if not v.dip:
            v2 = PoolEpisodeResult(**{**v.__dict__, "dip": True, "t_rec": 0, "censored": False})
            res_c[k] = v2
        else:
            res_c[k] = v
    out["S-c"] = {"H1": h1(episodes, res_c), "H2-arbitrum": h2(episodes, res_c, "arbitrum"), "H2-base": h2(episodes, res_c, "base")}
    eps_e = [e for e in episodes if not (e.long_cluster or e.pre_in_prior_recovery)]
    out["S-e"] = {"H1": h1(eps_e, results), "H2-arbitrum": h2(eps_e, results, "arbitrum"), "H2-base": h2(eps_e, results, "base")}
    out["S-f"] = {"H1": h1(episodes, results, chains=("arbitrum", "base"))}
    out["S-g"] = {"H1": h1(episodes, results, chains=("ethereum", "arbitrum"))}
    if results_by_z:
        out["S-h"] = {z: {"H1": h1(episodes, r), "H2-arbitrum": h2(episodes, r, "arbitrum"), "H2-base": h2(episodes, r, "base")}
                      for z, r in results_by_z.items()}
    strata = {}
    for p in build_h1_pairs(results, episodes):
        if p.valid:
            strata.setdefault(p.stratum, []).append(p.r)
    out["S-j"] = {"H1_stratified": stratified_signed_rank(strata)}
    return out
