"""Rules added before the code freeze (17 Oct 2026 ruling): D7 band geometry, D8 counters and the re-check after a
same-block return (R15), depth refusing dirty/failed states, explicit result flags, Holm with NaN, scipy mandatory,
the R1 net-map route."""
import math

import pytest

import pipeline.analysis as analysis
from pipeline.common import POOLS, Q96, sqrt_price_x96_from_usd, tick_at_sqrt_ratio, sqrt_ratio_at_tick
from pipeline.depth import depth
from pipeline.episodes import HORIZON_MIN
from pipeline.reconstruct import (BAND_Z_MAX, BandSnapshot, Event, ReconstructionError, band_tick_bounds, net_map_from_events,
                                  replay_window, state_from_full_replay, state_from_net_map, state_from_snapshot)
from pipeline.resilience_score import PoolEpisodeResult, evaluate, holm
from pipeline.sampling import ZS, Z_PRIMARY_INDEX, sample_depths
from tests.synth import M0, depth_series, make_episode, single_range_state

BASE = POOLS["base:0.05%"]
ETH = POOLS["ethereum:0.3%"]
P = 3000.0
L = 4_000_000_000_000_000


def _snap_state(pool, p, liq=L, nets=None):
    sp = sqrt_price_x96_from_usd(p, pool)
    t = tick_at_sqrt_ratio(sp / Q96)
    lo, hi = band_tick_bounds(sp, pool)
    return state_from_snapshot(pool, BandSnapshot(10, sp, t, liq, nets or {}, lo, hi))


def _swap(block, idx, p, pool, liq=L):
    sp = sqrt_price_x96_from_usd(p, pool)
    return Event(block, idx, "swap", sqrt_price_x96=sp, liquidity=liq, tick=tick_at_sqrt_ratio(sp / Q96))


# ----------------------------------------------------------------------------- D7: band geometry at the Z = 2% edge
@pytest.mark.parametrize("pool", [BASE, ETH])
def test_z_edge_forces_reread_between_six_and_eight_percent(pool):
    """A block-end price at -5.95% is inside the +/-6% drift rule, but its +/-2% depth band reaches -7.8%..-4.1%; the
    read band (+/-8% in p, widened to the spacing) covers that, yet at -6.1% the 2% band leaves it: D7 re-reads."""
    st = _snap_state(pool, P)
    reads = []

    def reader(block):
        reads.append(block)
        sp = st.sqrt_price_x96
        lo, hi = band_tick_bounds(sp, pool)
        return BandSnapshot(block, sp, st.tick, st.liquidity, {}, lo, hi)

    replay_window(st, [_swap(11, 0, P * 0.955, pool)], reader)       # -4.5%: 2% band stays inside -> nothing
    assert st.band_reread_count == 0 and st.reread_reason() is None
    replay_window(st, [_swap(12, 0, P * 0.941, pool)], reader)       # -5.9%: drift rule silent, z_edge may fire
    reasons = [r["reason"] for r in st.rereads]
    assert all(r == "z_edge" for r in reasons)
    st2 = _snap_state(pool, P)
    st2.sqrt_price_x96 = sqrt_price_x96_from_usd(P * 0.935, pool)    # -6.5%: drift wins the reason order
    st2.tick = tick_at_sqrt_ratio(st2.sqrt_price_x96 / Q96)
    assert st2.reread_reason() == "drift"
    for z in ZS:                                                       # after any re-read every Z fits again
        depth(st, z)


def test_sample_depths_loses_only_the_secondary_z_at_the_edge():
    st = _snap_state(BASE, P)
    # narrow the known band so that Z = 2% does not fit but Z = 1% does
    lo, hi = band_tick_bounds(st.sqrt_price_x96, BASE, half_width=0.015)
    st.band_lo_tick, st.band_hi_tick = lo, hi
    rec = sample_depths(st, 123)
    assert rec[Z_PRIMARY_INDEX] > 0 and rec[0] > 0 and rec[2] is None
    assert rec[5] == 123 and rec[4] == pytest.approx(P, rel=1e-9) and rec[3] > 0
    st.band_lo_tick, st.band_hi_tick = band_tick_bounds(st.sqrt_price_x96, BASE, half_width=0.005)
    with pytest.raises(ReconstructionError):
        sample_depths(st, 124)                                         # the primary Z must succeed


# ----------------------------------------------------------------------------- R3: depth refuses dirty / failed states
def test_depth_refuses_dirty_and_failed_states():
    st = _snap_state(BASE, P)
    st.band_dirty = True
    with pytest.raises(ReconstructionError):
        depth(st, 0.01)
    st.band_dirty = False
    st.band_read_failed = True
    with pytest.raises(ReconstructionError):
        depth(st, 0.01)
    st.band_read_failed = False
    assert depth(st, 0.01).d_z_usd > 0


# ----------------------------------------------------------------------------- D8 counters and R15 re-check
def test_checked_unchecked_counters_and_recheck_after_same_block_return():
    pool = BASE
    st = _snap_state(pool, P)
    lo = st.band_lo_tick
    outside = ((lo - 50) // pool.spacing) * pool.spacing
    reads = []

    def reader(block):
        reads.append(block)
        sp = st.sqrt_price_x96
        l2, h2 = band_tick_bounds(sp, pool)
        return BandSnapshot(block, sp, tick_at_sqrt_ratio(sp / Q96), st.liquidity, {}, l2, h2)

    sp_out = int(sqrt_ratio_at_tick(outside - 5) * Q96) + 1
    evs = [_swap(11, 0, P * 0.99, pool),                                                   # in-band: checked
           Event(11, 1, "swap", sqrt_price_x96=sp_out, liquidity=L + L // 3, tick=outside - 5),  # exit: unchecked
           _swap(11, 2, P * 0.98, pool, liq=L),                                            # return: unchecked, adopted
           _swap(11, 3, P * 0.985, pool, liq=L)]                                           # in-band again: CHECKED (R15)
    replay_window(st, evs, reader)
    assert (st.swaps_checked, st.swaps_unchecked) == (2, 2)
    assert reads == [11] and st.rereads[0]["reason"] == "dirty" and not st.band_dirty
    # the R15 re-check catches a wrong liquidity after the return inside the same block
    st2 = _snap_state(pool, P)
    with pytest.raises(ReconstructionError):
        replay_window(st2, evs[:3] + [_swap(11, 3, P * 0.985, pool, liq=L + 1)], reader)
    # R1 (full map): every swap is checked, none adopted
    full = state_from_full_replay(pool, [Event(1, 0, "initialize", sqrt_price_x96=sqrt_price_x96_from_usd(P, pool), tick=st.tick),
                                         Event(2, 0, "mint", tick_lower=st.tick - 20000 - (st.tick - 20000) % 10,
                                               tick_upper=st.tick + 20000 - (st.tick + 20000) % 10, amount=L)])
    full.apply(_swap(3, 0, P * 0.99, pool))
    assert (full.swaps_checked, full.swaps_unchecked) == (1, 0)


# ----------------------------------------------------------------------------- R1 net-map route (depth driver)
def test_net_map_state_equals_full_replay():
    pool = ETH
    sp0 = sqrt_price_x96_from_usd(P, pool)
    t0 = tick_at_sqrt_ratio(sp0 / Q96)
    evs = [Event(1, 0, "initialize", sqrt_price_x96=sp0, tick=t0),
           Event(2, 0, "mint", tick_lower=((t0 - 5000) // 60) * 60, tick_upper=((t0 + 5000) // 60) * 60, amount=L),
           Event(3, 0, "mint", tick_lower=((t0 - 100) // 60) * 60, tick_upper=((t0 + 100) // 60) * 60, amount=L // 4),
           Event(4, 0, "burn", tick_lower=((t0 - 100) // 60) * 60, tick_upper=((t0 + 100) // 60) * 60, amount=L // 8)]
    full = state_from_full_replay(pool, evs)
    nets = net_map_from_events(evs, pool, up_to_block=4)
    st = state_from_net_map(pool, nets, full.sqrt_price_x96, full.tick)
    assert st.liquidity == full.liquidity == L + L // 8 and st.liquidity_net == full.liquidity_net
    assert depth(st, 0.01).d_z_usd == pytest.approx(depth(full, 0.01).d_z_usd, rel=1e-12)
    assert net_map_from_events(evs, pool, up_to_block=2) == {evs[1].tick_lower: L, evs[1].tick_upper: -L}


# ----------------------------------------------------------------------------- R4: explicit flags, creation block, coverage
def test_result_flags_are_explicit_and_creation_block_marks_absent():
    ep = make_episode(1, M0 + 1000, M0 + 1000)
    series = depth_series(ep, 100.0, ep.t0, 40.0, ep.t1_last + 90)
    with pytest.raises(TypeError):
        evaluate(series, ep, "base:0.05%")                       # flags are required keywords
    r = evaluate(series, ep, "base:0.05%", local_shock=True, outage=False, creation_block=500, b0=400)
    assert r.pool_absent and r.t_rec == 90
    r2 = evaluate(series, ep, "base:0.05%", local_shock=False, outage=True, creation_block=300, b0=400)
    assert not r2.pool_absent and not r2.local_shock and r2.outage
    assert PoolEpisodeResult("x", 1, 1.0, 1.0, True, 0, False, None).local_shock is False
    del series[ep.t0 + 7]
    with pytest.raises(ValueError):
        evaluate(series, ep, "base:0.05%", local_shock=True, outage=False)   # R10: the series must cover the window


# ----------------------------------------------------------------------------- R9 and R6
def test_holm_keeps_nan_out_of_the_family():
    adj = holm([0.02, float("nan")])
    assert adj[0] == pytest.approx(0.02) and math.isnan(adj[1])
    assert holm([0.01, 0.04]) == pytest.approx([0.02, 0.04])


def test_confirmatory_refuses_without_scipy(monkeypatch):
    monkeypatch.setattr(analysis, "SCIPY_AVAILABLE", False)
    with pytest.raises(RuntimeError):
        analysis.run_confirmatory({}, [])
