import math

import numpy as np
import pytest

from pipeline.analysis import (MEI, N_FLOOR, build_h1_pairs, build_h2_pairs, episode_stats, pair_r, run_confirmatory,
                               run_sensitivities, run_test)
from pipeline.episodes import HORIZON_MIN
from pipeline.resilience_score import (SCIPY_AVAILABLE, PoolEpisodeResult, bootstrap_median_ratio, evaluate, holm,
                                       recovery_time, sign_test, stratified_signed_rank, wilcoxon_signed_rank)
from tests.synth import M0, depth_series, make_episode


# ----------------------------------------------------------------------------- metrics (6.3)
def test_recovery_time_with_persistence_and_known_answer():
    ep = make_episode(1, M0 + 1000, M0 + 1000)
    series = depth_series(ep, d_pre=100.0, dip_at=ep.t0, dip_val=40.0, recover_at=ep.t1_last + 90)
    r = evaluate(series, ep, "base:0.05%", local_shock=True, outage=False)
    assert r.d_pre == 100.0 and r.d_min == 40.0 and r.dip and not r.censored
    assert r.t_rec == 90 and r.t50 == 90


def test_persistence_blip_moves_recovery():
    ep = make_episode(1, M0 + 1000, M0 + 1000)
    extra = {ep.t1_last + 95: 80.0}   # a one-minute drop below 90% five minutes after the first qualifying minute
    series = depth_series(ep, 100.0, ep.t0, 40.0, ep.t1_last + 90, extra)
    r = evaluate(series, ep, "base:0.05%", local_shock=True, outage=False)
    assert r.t_rec == 96


def test_censoring_at_horizon_and_truncation():
    ep = make_episode(1, M0 + 1000, M0 + 1000)
    series = depth_series(ep, 100.0, ep.t0, 40.0, ep.window_end + 1)   # never recovers
    r = evaluate(series, ep, "base:0.05%", local_shock=True, outage=False)
    assert r.censored and r.t_rec == HORIZON_MIN
    ep2 = make_episode(2, M0 + 1000, M0 + 1000)
    ep2.truncated_by_next, ep2.censor_min = True, 500
    series2 = depth_series(ep2, 100.0, ep2.t0, 40.0, ep2.t1_last + 600)   # recovers at 600 > 500
    r2 = evaluate(series2, ep2, "base:0.05%", local_shock=True, outage=False)
    assert r2.censored and r2.t_rec == 500


def test_no_dip_when_drop_is_shallow():
    ep = make_episode(1, M0 + 1000, M0 + 1000)
    series = depth_series(ep, 100.0, ep.t0, 80.0, ep.t1_last + 90)      # 20% drop < 25%
    r = evaluate(series, ep, "base:0.05%", local_shock=True, outage=False)
    assert not r.dip and r.t_rec == 90


def test_recovery_exactly_zero_minutes():
    ep = make_episode(1, M0 + 1000, M0 + 1000)
    series = depth_series(ep, 100.0, ep.t0, 40.0, ep.t1_last)            # back by t1_last
    t, c = recovery_time(series, ep, 100.0)
    assert t == 0 and not c
    assert pair_r(0, 0) == 0.0 and pair_r(0, 4320) == math.log(1 / 4321)


# ----------------------------------------------------------------------------- statistics (7.5, 7.9, S-d)
def test_wilcoxon_exact_small_sample_known_value():
    d = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = wilcoxon_signed_rank(d)
    assert res["n"] == 5 and res["w_plus"] == 15.0
    assert res["p"] == pytest.approx(0.0625, abs=1e-12)   # all positive, n=5: 2/32
    res2 = wilcoxon_signed_rank([0.0, 1.0, -1.5, 2.0, -0.5, 3.0, 0.0])
    assert res2["n"] == 5


@pytest.mark.skipif(not SCIPY_AVAILABLE, reason="scipy not installed; fallback used (prereg names scipy for the frozen code)")
def test_fallback_matches_scipy_when_available():
    rng = np.random.default_rng(1)
    for n in (8, 25, 40):
        d = list(rng.normal(0.3, 1.0, n))            # continuous draws: no ties, exact regime for both
        fb = wilcoxon_signed_rank(d, impl="fallback")
        sp = wilcoxon_signed_rank(d, impl="scipy")
        assert fb["method"] == "exact" and sp["impl"] == "scipy"
        assert fb["w_plus"] == sp["w_plus"]
        assert fb["p"] == pytest.approx(sp["p"], rel=1e-9)
    big = list(rng.normal(0.1, 1.0, 80))             # normal-approximation regime: agree to the approximation's precision
    assert wilcoxon_signed_rank(big, impl="fallback")["p"] == pytest.approx(wilcoxon_signed_rank(big, impl="scipy")["p"], rel=1e-6)


def test_stratified_single_stratum_reduces_to_centred_signed_rank():
    d = [0.5, -0.2, 1.1, 0.7, -0.9, 0.3, 1.4, 0.05, -0.4, 0.8, 0.6, 1.0]
    n = len(d)
    res = stratified_signed_rank({"only": d})
    w = wilcoxon_signed_rank(d)
    assert res["strata"]["only"]["w_plus"] == pytest.approx(w["w_plus"])
    assert res["T"] == pytest.approx((w["w_plus"] - n * (n + 1) / 4) / (n + 1))


def test_sign_test_and_holm():
    assert sign_test([1, 1, 1, 1, 1, 1])["p"] == pytest.approx(2 / 64)
    assert holm([0.01, 0.04]) == pytest.approx([0.02, 0.04])
    assert holm([0.04, 0.01]) == pytest.approx([0.04, 0.02])


def test_bootstrap_is_deterministic_and_bracketing():
    pairs = [[0.5, 0.4], [0.6], [0.45, 0.5, 0.55], [0.3], [0.7, 0.65]]
    lo, hi = bootstrap_median_ratio(pairs, b=2000, seed=20261017)
    lo2, hi2 = bootstrap_median_ratio(pairs, b=2000, seed=20261017)
    assert (lo, hi) == (lo2, hi2) and lo <= math.exp(0.5) <= hi


# ----------------------------------------------------------------------------- pairing and verdicts (7.1-7.7)
def _res(pool_key, eid, t_rec, censored=False, dip=True, local=True, outage=False, absent=False):
    return PoolEpisodeResult(pool_key, eid, 100.0, 40.0, dip, t_rec, censored, None, local_shock=local, outage=outage,
                             pool_absent=absent)


def _full_results(n_eps, slow_factor=2.0, base_t=60):
    """0.05% pools recover in base_t min, 0.3% pools in slow_factor x base_t; L2s 20% faster than Ethereum."""
    res, eps = {}, []
    for i in range(1, n_eps + 1):
        eps.append(make_episode(i, M0 + 10000 * i, M0 + 10000 * i))
        jitter = 1.0 + 0.01 * i
        for chain, f in (("ethereum", 1.0), ("arbitrum", 0.8), ("base", 0.8)):
            res[(f"{chain}:0.05%", i)] = _res(f"{chain}:0.05%", i, int(base_t * f * jitter))
            res[(f"{chain}:0.3%", i)] = _res(f"{chain}:0.3%", i, int(base_t * slow_factor * f * jitter))
    return res, eps


def test_pairs_validity_reasons_in_declared_order():
    res, eps = _full_results(2)
    res[("base:0.3%", 1)] = _res("base:0.3%", 1, 100, dip=False, local=False)   # local_shock checked before dip
    res[("arbitrum:0.05%", 2)] = _res("arbitrum:0.05%", 2, 100, outage=True, dip=False)
    pairs = build_h1_pairs(res, eps)
    by = {(p.episode_id, p.stratum): p for p in pairs}
    assert by[(1, "base")].reason == "local_shock" and not by[(1, "base")].valid
    assert by[(2, "arbitrum")].reason == "outage"
    assert by[(1, "ethereum")].valid and by[(1, "ethereum")].censor_state == "uncensored"
    del res[("base:0.05%", 2)]
    pairs2 = build_h1_pairs(res, eps)
    assert [p for p in pairs2 if p.episode_id == 2 and p.stratum == "base"][0].reason == "pool_absent"


def test_episode_statistics_censoring_and_rank_only():
    res, eps = _full_results(3)
    # episode 2: every pair one-censored (0.3% pools censored at 4320) -> rank-only with sign from d_val
    for c in ("ethereum", "arbitrum", "base"):
        res[(f"{c}:0.3%", 2)] = _res(f"{c}:0.3%", 2, HORIZON_MIN, censored=True)
    # episode 3: ethereum pair one-censored, others uncensored -> d_rank = mean of the two uncensored pairs
    res[("ethereum:0.3%", 3)] = _res("ethereum:0.3%", 3, HORIZON_MIN, censored=True)
    stats, counts = episode_stats(build_h1_pairs(res, eps))
    s = {x.episode_id: x for x in stats}
    assert not s[1].rank_only and s[1].n_uncensored == 3
    assert s[2].rank_only and s[2].d_rank > 0 and s[2].d_rank == pytest.approx(max(abs(s[1].d_rank), abs(s[3].d_rank)) + 1.0)
    assert s[3].n_uncensored == 2 and s[3].d_rank == pytest.approx(np.mean([p for p in s[3].pairs_r][1:]))
    assert counts["one_censored_episodes"] == 1 and counts["uncensored_episodes"] == 2


def test_zero_differences_dropped_and_counted():
    res, eps = _full_results(2, slow_factor=1.0)   # identical recovery times -> d = 0 everywhere
    stats, counts = episode_stats(build_h1_pairs(res, eps))
    t = run_test("H1", stats, counts, b=200)
    assert t.n == 0 and t.zeros_dropped == 2 and t.verdict == "Underpowered"


def test_confirmatory_yes_verdict_on_planted_effect():
    res, eps = _full_results(25, slow_factor=2.0)
    out = run_confirmatory(res, eps, b=500)
    h1 = out["H1"]
    assert h1.n == 25 and h1.median_ratio == pytest.approx(math.exp(math.log(121 / 61)), rel=0.05)
    assert h1.p < 0.001 and h1.verdict == "Yes"
    # H2: L2s 20% faster than Ethereum -> ratio ~1.25 < 1.5 -> never "Yes"; Holm applied across the two
    assert out["H2-arbitrum"].verdict in ("Null", "No") and out["H2-base"].verdict in ("Null", "No")
    assert out["H2-arbitrum"].p_adj >= out["H2-arbitrum"].p


def test_no_verdict_when_ci_below_mei():
    res, eps = _full_results(30, slow_factor=1.1)   # true ratio 1.1 with tiny jitter -> CI well below 1.5
    out = run_confirmatory(res, eps, b=500)
    assert out["H1"].verdict == "No" and out["H1"].ci90[1] < MEI


def test_underpowered_below_floor():
    res, eps = _full_results(N_FLOOR - 1)
    assert run_confirmatory(res, eps, b=200)["H1"].verdict == "Underpowered"


def test_sensitivities_run_without_verdicts():
    res, eps = _full_results(22)
    flags = {e.episode_id: {"incentive_v4_pull": e.episode_id % 5 == 0} for e in eps}
    out = run_sensitivities(res, eps, flags, b=200)
    assert set(out) >= {"S-a", "S-b", "S-c", "S-e", "S-f", "S-g", "S-j"}
    assert "verdict" not in out["S-a"]["H1"]
    assert out["S-a"]["H1"]["n"] == 22 - 4   # episodes 5,10,15,20 excluded
    assert out["S-f"]["H1"]["n"] == 22 and out["S-j"]["H1_stratified"]["strata"]["ethereum"]["n"] == 22
