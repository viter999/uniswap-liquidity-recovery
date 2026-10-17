"""Per pool-episode recovery metrics (prereg v0.3 sections 6.3-6.4) and the statistics (7.5, 7.9, S-d).

The signed-rank test is scipy.stats.wilcoxon when scipy is installed (the frozen spec); until then an exact pure-Python
implementation with the same conventions (zeros dropped, average ranks, exact p without ties for n <= 50, normal
approximation without continuity correction otherwise) stands in, and a test compares the two when both exist.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .episodes import Episode, HORIZON_MIN, PERSIST_MIN

try:  # the frozen spec names scipy; absence is recorded, not hidden
    from scipy import stats as _scipy_stats  # type: ignore
    SCIPY_AVAILABLE = True
except Exception:  # noqa: BLE001
    _scipy_stats = None
    SCIPY_AVAILABLE = False

RECOVERY_LEVEL = 0.9
DIP_LEVEL = 0.75
SEED = 20261017
BOOT_B = 10_000


@dataclass
class PoolEpisodeResult:
    pool_key: str
    episode_id: int
    d_pre: float
    d_min: float
    dip: bool
    t_rec: int                 # minutes from t1_last; censored value when censored
    censored: bool
    t50: Optional[int]
    local_shock: bool = False       # explicit: a pool-episode is a valid pair member only if the driver sets this (7.1a)
    outage: bool = False
    chain_degraded: bool = False
    header_gap: bool = False
    band_reread_count: int = 0
    band_read_failed: bool = False
    pool_absent: bool = False
    replenishment_share: Optional[float] = None


# ----------------------------------------------------------------------------- metrics (6.3)
def _values(series: Dict[int, float], lo: int, hi: int) -> List[float]:
    return [series[m] for m in range(lo, hi + 1) if m in series]


def pre_level(series: Dict[int, float], ep: Episode) -> float:
    lo, hi = ep.pre_window
    v = _values(series, lo, hi)
    if not v:
        raise ValueError("no samples in the pre-shock window")
    return float(np.median(v))


def dip_level(series: Dict[int, float], ep: Episode) -> float:
    lo, hi = ep.dip_window
    v = _values(series, lo, hi)
    if not v:
        raise ValueError("no samples in the dip window")
    return float(min(v))


def _first_persistent(series: Dict[int, float], start: int, last_start: int, level: float) -> Optional[int]:
    """First t in [start, last_start] with series >= level on every minute of [t, t+15). Missing minutes fail the test."""
    for t in range(start, last_start + 1):
        ok = True
        for s in range(t, t + PERSIST_MIN):
            v = series.get(s)
            if v is None or v < level:
                ok = False
                break
        if ok:
            return t
    return None


def recovery_time(series: Dict[int, float], ep: Episode, d_pre: float) -> Tuple[int, bool]:
    """T_rec in minutes from t1_last with the 15-min persistence; censored at ep.censor_min (4320 or truncated)."""
    horizon = ep.censor_min
    t = _first_persistent(series, ep.t1_last, ep.t1_last + horizon - 1, RECOVERY_LEVEL * d_pre)
    if t is None:
        return horizon, True
    return t - ep.t1_last, False


def half_life(series: Dict[int, float], ep: Episode, d_pre: float, d_min: float) -> Optional[int]:
    level = d_min + 0.5 * (d_pre - d_min)
    t = _first_persistent(series, ep.t1_last, ep.t1_last + ep.censor_min - 1, level)
    return None if t is None else t - ep.t1_last


def evaluate(series: Dict[int, float], ep: Episode, pool_key: str, *, local_shock: bool, outage: bool,
             chain_degraded: bool = False, header_gap: bool = False, band_reread_count: int = 0,
             band_read_failed: bool = False, creation_block: Optional[int] = None, b0: Optional[int] = None,
             replenishment_share: Optional[float] = None) -> PoolEpisodeResult:
    """Per pool-episode metrics (6.3). The validity flags are explicit keyword arguments: nothing defaults to 'valid'.

    pool_absent (7.1a) = creation block > b0, or D_pre = 0."""
    missing = [m for m in range(ep.window_start, ep.window_end + 1) if m not in series]
    if missing:
        raise ValueError(f"depth series does not cover the window: {len(missing)} minutes missing (first {missing[0]})")
    d_pre = pre_level(series, ep)
    d_min = dip_level(series, ep)
    dip = d_min < DIP_LEVEL * d_pre
    t_rec, censored = recovery_time(series, ep, d_pre)
    t50 = half_life(series, ep, d_pre, d_min)
    absent = (d_pre == 0.0) or (creation_block is not None and b0 is not None and creation_block > b0)
    return PoolEpisodeResult(pool_key, ep.episode_id, d_pre, d_min, dip, t_rec, censored, t50,
                             local_shock=local_shock, outage=outage, chain_degraded=chain_degraded, header_gap=header_gap,
                             band_reread_count=band_reread_count, band_read_failed=band_read_failed, pool_absent=absent,
                             replenishment_share=replenishment_share)


# ----------------------------------------------------------------------------- signed-rank test (7.5)
def _ranks_avg(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x))
    i = 0
    sx = x[order]
    while i < len(x):
        j = i
        while j + 1 < len(x) and sx[j + 1] == sx[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    return ranks


def _exact_wplus_pvalue(w_plus: float, n: int) -> float:
    """Two-sided exact p for W+ under H0 (no ties), via the distribution of signed-rank sums."""
    counts = {0: 1}
    for r in range(1, n + 1):
        nxt = dict(counts)
        for s, c in counts.items():
            nxt[s + r] = nxt.get(s + r, 0) + c
        counts = nxt
    total = 2 ** n
    mean = n * (n + 1) / 4.0
    dev = abs(w_plus - mean)
    tail = sum(c for s, c in counts.items() if abs(s - mean) >= dev - 1e-9)
    return min(1.0, tail / total)


def wilcoxon_signed_rank(d: Sequence[float], impl: str = "auto") -> dict:
    """Zeros dropped, average ranks, exact p (no ties, n<=50) else normal approximation without continuity correction.

    impl: "auto" = scipy when installed (the frozen spec, 7.5) else the fallback; "scipy"; "fallback".
    W+ (sum of positive ranks) is always computed here; scipy's reported statistic is the smaller rank sum, so only
    its p-value is used."""
    x = np.asarray([v for v in d if v != 0.0], dtype=float)
    n = len(x)
    if n == 0:
        return {"n": 0, "w_plus": float("nan"), "p": float("nan"), "method": "none", "impl": "none"}
    ranks = _ranks_avg(np.abs(x))
    w_plus = float(ranks[x > 0].sum())
    use_scipy = (impl == "scipy") or (impl == "auto" and SCIPY_AVAILABLE)
    if use_scipy:
        if not SCIPY_AVAILABLE:
            raise RuntimeError("scipy requested but not installed")
        res = _scipy_stats.wilcoxon(x, zero_method="wilcox", correction=False, alternative="two-sided", method="auto")
        return {"n": n, "w_plus": w_plus, "p": float(res.pvalue), "method": "scipy", "impl": "scipy"}
    ties = len(set(np.abs(x))) < n
    if n <= 50 and not ties:
        return {"n": n, "w_plus": w_plus, "p": _exact_wplus_pvalue(w_plus, n), "method": "exact", "impl": "fallback"}
    mean = n * (n + 1) / 4.0
    var = n * (n + 1) * (2 * n + 1) / 24.0
    _, cnt = np.unique(np.abs(x), return_counts=True)
    var -= float(((cnt ** 3 - cnt) / 48.0).sum())
    z = (w_plus - mean) / math.sqrt(var)
    p = 2.0 * (1.0 - _norm_cdf(abs(z)))
    return {"n": n, "w_plus": w_plus, "p": p, "method": "normal", "impl": "fallback"}


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ----------------------------------------------------------------------------- stratified statistic (7.9)
def stratified_signed_rank(strata: Dict[str, Sequence[float]]) -> dict:
    """T = sum_s w_s (W_s - n_s(n_s+1)/4), w_s = 1/(n_s+1); Var0 = sum w_s^2 [n(n+1)(2n+1)/24 - sum(t^3-t)/48]; normal p."""
    t_stat, var = 0.0, 0.0
    detail = {}
    for s, d in strata.items():
        x = np.asarray([v for v in d if v != 0.0], dtype=float)
        n = len(x)
        if n == 0:
            continue
        ranks = _ranks_avg(np.abs(x))
        w_s = float(ranks[x > 0].sum())
        _, cnt = np.unique(np.abs(x), return_counts=True)
        v_s = n * (n + 1) * (2 * n + 1) / 24.0 - float(((cnt ** 3 - cnt) / 48.0).sum())
        w = 1.0 / (n + 1)
        t_stat += w * (w_s - n * (n + 1) / 4.0)
        var += w * w * v_s
        detail[s] = {"n": n, "w_plus": w_s}
    if var == 0.0:
        return {"T": t_stat, "var": var, "z": float("nan"), "p": float("nan"), "strata": detail}
    z = t_stat / math.sqrt(var)
    return {"T": t_stat, "var": var, "z": z, "p": 2.0 * (1.0 - _norm_cdf(abs(z))), "strata": detail}


# ----------------------------------------------------------------------------- sign test (S-d), Holm, bootstrap (7.5)
def sign_test(d: Sequence[float]) -> dict:
    pos = sum(1 for v in d if v > 0)
    neg = sum(1 for v in d if v < 0)
    n = pos + neg
    if n == 0:
        return {"n": 0, "pos": 0, "neg": 0, "p": float("nan")}
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return {"n": n, "pos": pos, "neg": neg, "p": min(1.0, 2.0 * tail)}


def holm(pvals: Sequence[float]) -> List[float]:
    """Holm step-down over the DEFINED p-values; a NaN (test not run, n = 0) stays NaN and does not count towards m."""
    defined = [i for i, p in enumerate(pvals) if not math.isnan(p)]
    m = len(defined)
    order = sorted(defined, key=lambda i: pvals[i])
    adj = [float("nan")] * len(pvals)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvals[i])
        adj[i] = min(1.0, running)
    return adj


def bootstrap_median_ratio(episode_pairs: Sequence[Sequence[float]], b: int = BOOT_B, seed: int = SEED) -> Tuple[float, float]:
    """Percentile 5/95 of exp(median of per-episode means) over B resamples of episodes (pairs kept together)."""
    rng = np.random.default_rng(seed)
    means = np.asarray([float(np.mean(p)) for p in episode_pairs])
    n = len(means)
    if n == 0:
        return float("nan"), float("nan")
    idx = rng.integers(0, n, size=(b, n))
    med = np.median(means[idx], axis=1)
    lo, hi = np.percentile(np.exp(med), [5, 95])
    return float(lo), float(hi)


def resilience_score(t_recs: Sequence[int], b: int = BOOT_B, seed: int = SEED) -> dict:
    """Pool resilience score: median T_rec with a percentile-bootstrap CI (deliverable 2)."""
    x = np.asarray(t_recs, dtype=float)
    if len(x) == 0:
        return {"median": float("nan"), "ci": (float("nan"), float("nan"))}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(b, len(x)))
    med = np.median(x[idx], axis=1)
    return {"median": float(np.median(x)), "ci": tuple(float(v) for v in np.percentile(med, [5, 95]))}
