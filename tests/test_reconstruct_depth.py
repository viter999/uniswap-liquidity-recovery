import math

import pytest

from pipeline.common import POOLS, Q96, sqrt_price_x96_from_usd, sqrt_ratio_at_tick, tick_at_sqrt_ratio
from pipeline.depth import depth
from pipeline.reconstruct import (BandSnapshot, Event, ReconstructionError, band_tick_bounds, replay_window,
                                  state_from_full_replay, state_from_snapshot)
from tests.synth import single_range_state

ETH = POOLS["ethereum:0.05%"]   # token0 = USDC (inverted)
BASE = POOLS["base:0.05%"]      # token0 = WETH (direct)
P = 3000.0
L = 4_000_000_000_000_000       # 4e15 raw liquidity


def closed_form_single_range(p: float, liq: float, z: float, pool) -> float:
    """D_Z for one position covering the band: selling to p(1-Z) and buying to p(1+Z) with constant L."""
    sp = sqrt_price_x96_from_usd(p, pool) / Q96
    if pool.token0_is_weth:
        d_sell = liq * (sp - sp * math.sqrt(1 - z)) * 1e-6                     # amount1 (USDC)
        d_buy = liq * (1 / sp - 1 / (sp * math.sqrt(1 + z))) * 1e-18           # amount0 (WETH)
    else:
        hi = sp / math.sqrt(1 - z)
        d_sell = liq * (1 / sp - 1 / hi) * 1e-6                                # amount0 (USDC)
        lo = sp / math.sqrt(1 + z)
        d_buy = liq * (sp - lo) * 1e-18                                        # amount1 (WETH)
    return d_sell + p * d_buy


@pytest.mark.parametrize("pool", [ETH, BASE])
@pytest.mark.parametrize("z", [0.005, 0.01, 0.02])
def test_single_range_depth_matches_closed_form(pool, z):
    st = single_range_state(pool, P, L)
    d = depth(st, z)
    assert d.p == pytest.approx(P, rel=1e-9)
    assert d.d_z_usd == pytest.approx(closed_form_single_range(P, L, z, pool), rel=1e-9)
    assert d.d_sell_usd > 0 and d.d_buy_eth > 0


def test_same_liquidity_both_orientations_same_depth():
    a = depth(single_range_state(ETH, P, L), 0.01)
    b = depth(single_range_state(BASE, P, L), 0.01)
    assert a.d_z_usd == pytest.approx(b.d_z_usd, rel=1e-9)
    assert a.int_l == pytest.approx(b.int_l, rel=1e-6)


def test_two_range_depth_sums_pieces():
    """Liquidity doubles below the current tick: D_sell must exceed the single-range value by the extra piece."""
    pool = BASE
    st = single_range_state(pool, P, L)
    sp = st.sqrt_ratio
    boundary = ((st.tick - 30) // pool.spacing) * pool.spacing       # an initialised tick 30-40 ticks below current
    st.liquidity_net[boundary] = -L                                   # crossing boundary upward removes L => below it L is 2L
    d = depth(st, 0.01)
    sb = sqrt_ratio_at_tick(boundary)
    s_lo = sp * math.sqrt(0.99)
    assert s_lo < sb < sp
    expected_sell = (L * (sp - sb) + 2 * L * (sb - s_lo)) * 1e-6
    assert d.d_sell_usd == pytest.approx(expected_sell, rel=1e-9)
    single = closed_form_single_range(P, L, 0.01, pool)
    assert d.d_z_usd > single


def test_depth_refuses_band_outside_snapshot():
    st = single_range_state(BASE, P, L)
    st.band_lo_tick, st.band_hi_tick = st.tick - 20, st.tick + 20    # only +/-20 ticks known
    with pytest.raises(ReconstructionError):
        depth(st, 0.01)                                              # +/-1% needs ~+/-100 ticks


def _swap_event(block, idx, st_sqrt_x96, pool, tick, liquidity):
    return Event(block, idx, "swap", sqrt_price_x96=st_sqrt_x96, liquidity=liquidity, tick=tick)


def test_r1_full_replay_and_r2_snapshot_agree_and_swap_check():
    pool = BASE
    sp0 = sqrt_price_x96_from_usd(P, pool)
    t0 = tick_at_sqrt_ratio(sp0 / Q96)
    lower, upper = ((t0 - 5000) // 10) * 10, ((t0 + 5000) // 10) * 10
    inner_lo, inner_hi = ((t0 - 50) // 10) * 10, ((t0 + 50) // 10) * 10
    events = [
        Event(1, 0, "initialize", sqrt_price_x96=sp0, tick=t0),
        Event(2, 0, "mint", tick_lower=lower, tick_upper=upper, amount=L),
        Event(3, 0, "mint", tick_lower=inner_lo, tick_upper=inner_hi, amount=L // 2),
    ]
    r1 = state_from_full_replay(pool, events)
    assert r1.liquidity == L + L // 2
    # a swap moving the price above inner_hi: active liquidity must drop by L//2 and the event must carry that value
    new_tick = inner_hi + 5
    new_sqrt = int(sqrt_ratio_at_tick(new_tick) * Q96) + 1
    ok = _swap_event(4, 0, new_sqrt, pool, new_tick, L)
    r1.apply(ok)
    assert r1.liquidity == L and r1.tick == new_tick
    bad = _swap_event(5, 0, new_sqrt, pool, new_tick, L + 1)
    with pytest.raises(ReconstructionError):
        r1.apply(bad)
    # R2: snapshot after block 3 (band around p), then replay the same swap -> identical state and depth
    lo, hi = band_tick_bounds(sp0, pool)
    snap = BandSnapshot(3, sp0, t0, L + L // 2, {inner_lo: L // 2, inner_hi: -(L // 2)}, lo, hi)
    r2 = state_from_snapshot(pool, snap)
    replay_window(r2, [ok], reader=None)
    assert r2.liquidity == r1.liquidity and r2.tick == r1.tick
    assert depth(r2, 0.01).d_z_usd == pytest.approx(depth(r1, 0.01).d_z_usd, rel=1e-12)


def test_band_reread_triggers_at_six_percent_and_logs():
    pool = BASE
    sp0 = sqrt_price_x96_from_usd(P, pool)
    t0 = tick_at_sqrt_ratio(sp0 / Q96)
    lo, hi = band_tick_bounds(sp0, pool)
    st = state_from_snapshot(pool, BandSnapshot(10, sp0, t0, L, {}, lo, hi))
    reads = []

    def reader(block):
        sp = sqrt_price_x96_from_usd(P * 0.93, pool)
        t = tick_at_sqrt_ratio(sp / Q96)
        l2, h2 = band_tick_bounds(sp, pool)
        reads.append(block)
        return BandSnapshot(block, sp, t, L, {}, l2, h2)

    # a swap to -5%: inside +/-6%, no re-read
    sp5 = sqrt_price_x96_from_usd(P * 0.95, pool)
    replay_window(st, [Event(11, 0, "swap", sqrt_price_x96=sp5, liquidity=L, tick=tick_at_sqrt_ratio(sp5 / Q96))], reader)
    assert st.band_reread_count == 0 and reads == []
    # a swap to -7%: outside +/-6% -> re-read at the end of that block
    sp7 = sqrt_price_x96_from_usd(P * 0.93, pool)
    replay_window(st, [Event(12, 0, "swap", sqrt_price_x96=sp7, liquidity=L, tick=tick_at_sqrt_ratio(sp7 / Q96))], reader)
    assert st.band_reread_count == 1 and reads == [12] and st.rereads[0]["block"] == 12
    assert st.band_center_p == pytest.approx(P * 0.93, rel=1e-9)


def test_same_block_band_exit_and_return_forces_reread_without_false_mismatch():
    """Stage A finding (2023-01-14 window): a swap leaves the read band and a later swap in the SAME block returns;
    the walk back would cross ticks the snapshot never read, so the state must adopt the event value, mark itself
    dirty, skip the equality check, and re-read at block end."""
    pool = BASE
    sp0 = sqrt_price_x96_from_usd(P, pool)
    t0 = tick_at_sqrt_ratio(sp0 / Q96)
    lo, hi = band_tick_bounds(sp0, pool)
    # a position whose lower tick lies just OUTSIDE the band (unknown to the snapshot)
    outside = ((lo - 50) // pool.spacing) * pool.spacing
    st = state_from_snapshot(pool, BandSnapshot(10, sp0, t0, L, {}, lo, hi))
    reads = []

    def reader(block):
        sp = st.sqrt_price_x96
        t = tick_at_sqrt_ratio(sp / Q96)
        l2, h2 = band_tick_bounds(sp, pool)
        reads.append(block)
        return BandSnapshot(block, sp, t, st.liquidity, {}, l2, h2)

    sp_out = int(sqrt_ratio_at_tick(outside - 5) * Q96) + 1          # below the band
    sp_back = sqrt_price_x96_from_usd(P * 0.98, pool)                 # back inside, -2%
    t_back = tick_at_sqrt_ratio(sp_back / Q96)
    # chain truth: crossing `outside` downward added L//3 (position [outside, hi+...)) -> event liquidity L + L//3 below,
    # and L again once back above `outside`; the snapshot does not know `outside`, so only the event values are usable
    evs = [Event(11, 0, "swap", sqrt_price_x96=sp_out, liquidity=L + L // 3, tick=outside - 5),
           Event(11, 1, "swap", sqrt_price_x96=sp_back, liquidity=L, tick=t_back)]
    replay_window(st, evs, reader)                                    # must not raise a false mismatch
    assert reads == [11] and st.band_reread_count == 1 and not st.band_dirty
    assert st.liquidity == L and st.tick == t_back


def test_band_read_failure_drops_pool_episode():
    pool = BASE
    sp0 = sqrt_price_x96_from_usd(P, pool)
    t0 = tick_at_sqrt_ratio(sp0 / Q96)
    lo, hi = band_tick_bounds(sp0, pool)
    st = state_from_snapshot(pool, BandSnapshot(10, sp0, t0, L, {}, lo, hi))
    attempts = []

    def failing_reader(block):
        attempts.append(block)
        raise RuntimeError("rpc down")

    sp7 = sqrt_price_x96_from_usd(P * 1.07, pool)
    replay_window(st, [Event(12, 0, "swap", sqrt_price_x96=sp7, liquidity=L, tick=tick_at_sqrt_ratio(sp7 / Q96))], failing_reader)
    assert st.band_read_failed and len(attempts) == 3
