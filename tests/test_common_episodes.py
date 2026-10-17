import math

import numpy as np
import pytest

from pipeline import common as C
from pipeline.episodes import (ALTERNATE, PRIMARY, HORIZON_MIN, GAP_MIN, decide_primary, detect_episodes, confirmatory,
                               forward_fill, local_confirmed, n_conf)
from tests.synth import M0, flat_series, plant_move


# ----------------------------------------------------------------------------- orientation (5.1) and tick math
def test_price_orientation_same_usd_both_token_orders():
    eth = C.POOLS["ethereum:0.05%"]   # token0 = USDC
    base = C.POOLS["base:0.05%"]      # token0 = WETH
    for p in (1500.0, 3000.0, 4321.5):
        assert C.price_usd_per_eth(C.sqrt_price_x96_from_usd(p, eth), eth) == pytest.approx(p, rel=1e-9)
        assert C.price_usd_per_eth(C.sqrt_price_x96_from_usd(p, base), base) == pytest.approx(p, rel=1e-9)
    # a raw price move of +1% is a -1% USD move on the inverted pool and +1% on the direct pool
    s = C.sqrt_price_x96_from_usd(3000.0, eth)
    s_up = int(s * math.sqrt(1.01))
    assert C.price_usd_per_eth(s_up, eth) < 3000.0
    s2 = C.sqrt_price_x96_from_usd(3000.0, base)
    assert C.price_usd_per_eth(int(s2 * math.sqrt(1.01)), base) > 3000.0


def test_tick_math_roundtrip_and_bitmap():
    for t in (-887000, -200000, -1, 0, 1, 193456, 887000):
        assert C.tick_at_sqrt_ratio(C.sqrt_ratio_at_tick(t)) == t
    assert C.bitmap_position(-10, 10) == (-1, 255)
    assert C.bitmap_position(2560, 10) == (1, 0)
    assert C.floor_to_spacing(-7, 10) == -10 and C.ceil_to_spacing(-7, 10) == 0


def test_opstack_timestamp_formula_matches_probe_values():
    # values read on 2026-10-17 (probes/probe_genesis_20261017.txt)
    assert C.timestamp_from_block("base", 0) == 1686789347
    assert C.timestamp_from_block("base", 52602795) == 1791994937
    assert C.timestamp_from_block("unichain", 61346580) == 1792094939
    m = C.minute_of(1791994937)
    b = C.block_at_minute_end("base", m)
    assert C.timestamp_from_block("base", b) < 60 * (m + 1) <= C.timestamp_from_block("base", b + 1)
    assert C.block_at_minute_end_from_headers({10: 600, 11: 659, 12: 660}, 10) == 11


def test_int24_parse():
    assert C.parse_int24(format((1 << 256) - 1, "064x")) == -1
    assert C.parse_int24(format(193456, "064x")) == 193456


# ----------------------------------------------------------------------------- detection (5.2-5.4)
def test_forward_fill_carries_last_price():
    obs = {M0 + 2: 10.0, M0 + 5: 11.0}
    s = forward_fill(obs, M0, M0 + 8)
    assert np.isnan(s[0]) and np.isnan(s[1])
    assert list(s[2:5]) == [10.0, 10.0, 10.0] and list(s[5:]) == [11.0, 11.0, 11.0]


def test_planted_shocks_detected_with_known_times():
    n = 20 * 24 * 60
    pr = flat_series(n)
    plant_move(pr, at=3000, frac=-0.06, ramp=10)            # 6% drop over 10 min -> episode 1
    plant_move(pr, at=3000 + 24 * 60, frac=+0.055, ramp=5)  # 24 h later: within 48 h gap -> same episode
    plant_move(pr, at=3000 + 5 * 24 * 60, frac=+0.07, ramp=3)  # 5 days later -> episode 2
    eps = detect_episodes(M0, pr, PRIMARY)
    assert [e.episode_id for e in eps] == [1, 2]
    e1, e2 = eps
    # first trigger minute: the 60-min log change first reaches 5% during the ramp (drop reaches 5% at the 9th minute)
    assert e1.t1_first == M0 + 3000 + 8 and e1.t0 == e1.t1_first - 60
    assert e1.direction == "ETH_down" and e2.direction == "ETH_up"
    assert e1.t1_last >= M0 + 3000 + 24 * 60 and e1.long_cluster is False and e1.span_min < GAP_MIN
    assert e2.long_cluster is False
    # consecutive flags: e2 starts 5 days after e1's last trigger -> no truncation, no pre_in_prior_recovery
    assert not e1.truncated_by_next and not e2.pre_in_prior_recovery and e1.censor_min == HORIZON_MIN


def test_truncation_and_pre_in_prior_recovery_flags():
    n = 10 * 24 * 60
    pr = flat_series(n)
    plant_move(pr, at=1000, frac=-0.06)
    plant_move(pr, at=1000 + 49 * 60, frac=-0.06)   # 49 h later: new episode (gap >= 48 h) but inside e1's 72 h recovery
    eps = detect_episodes(M0, pr, PRIMARY)
    assert len(eps) == 2
    e1, e2 = eps
    assert e1.truncated_by_next and e1.censor_min == e2.t0 - e1.t1_last and e1.censor_min < HORIZON_MIN
    assert e2.pre_in_prior_recovery


def test_long_cluster_flag_and_alternate_params():
    n = 10 * 24 * 60
    pr = flat_series(n)
    for k in range(0, 3 * 24 * 60, 12 * 60):          # a 5% drop every 12 h for 3 days -> one long cluster
        plant_move(pr, at=1000 + k, frac=-0.055, ramp=5)
    eps = detect_episodes(M0, pr, PRIMARY)
    assert len(eps) == 1 and eps[0].long_cluster
    # the alternate set (3% over 30 min) has t0 = t1_first - 30
    eps_a = detect_episodes(M0, pr, ALTERNATE)
    assert eps_a and eps_a[0].t0 == eps_a[0].t1_first - 30


def test_confirmatory_membership_by_t1_first():
    pr = flat_series(5000)
    plant_move(pr, at=1000, frac=-0.06)
    eps = detect_episodes(M0, pr, PRIMARY)
    assert confirmatory(eps, M0 + 1000, M0 + 2000) == eps
    assert confirmatory(eps, M0 + 1100, M0 + 2000) == []


# ----------------------------------------------------------------------------- local confirmation (5.5) and decision (5.6)
def test_local_confirmation_requires_magnitude_and_sign():
    assert local_confirmed(3000.0, 3000.0 * math.exp(-0.045), -0.06, PRIMARY)      # 4.5% down, clock down
    assert not local_confirmed(3000.0, 3000.0 * math.exp(-0.035), -0.06, PRIMARY)  # 3.5% < 4%
    assert not local_confirmed(3000.0, 3000.0 * math.exp(+0.045), -0.06, PRIMARY)  # wrong sign
    assert local_confirmed(3000.0, 3000.0 * math.exp(-0.025), -0.04, ALTERNATE)    # 2.5% >= 2.4%


def test_nconf_and_switch_rule():
    pools = ["ethereum:0.3%", "arbitrum:0.05%", "arbitrum:0.3%", "base:0.05%", "base:0.3%"]
    flags = {i: {k: True for k in pools} for i in range(30)}
    flags[3]["base:0.3%"] = False
    assert n_conf(flags, pools) == 29
    assert decide_primary(47, 29, 101, 80).chosen == "5pct_60min"
    assert decide_primary(47, 23, 101, 80).chosen == "3pct_30min"
    assert decide_primary(47, 24, 101, 80).chosen == "5pct_60min"
