"""In-range USD depth D_Z and the band-integrated liquidity (prereg v0.3 section 6.2).

Per initialised tick range [i_lo, i_hi) with constant active liquidity L (raw units):
    amount0 = L * (1/sqrt_a - 1/sqrt_b),   amount1 = L * (sqrt_b - sqrt_a)
clipped to the band and split at the live sqrt price for the range containing it. USDC scaled by 1e-6, WETH by 1e-18.
D_sell = USDC received selling ETH until p reaches p(1-Z); D_buy = ETH received buying until p reaches p(1+Z),
valued at p; D_Z = D_sell + p * D_buy, fee-free, USDC = 1 USD.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .common import USDC_DECIMALS, WETH_DECIMALS, PoolSpec, sqrt_ratio_at_tick, tick_at_sqrt_ratio
from .reconstruct import PoolState, ReconstructionError

Z_PRIMARY = 0.01
Z_SECONDARY = (0.005, 0.02)


@dataclass(frozen=True)
class Depth:
    z: float
    p: float             # USD per ETH at the sample
    d_sell_usd: float    # USDC obtainable selling ETH down to p(1-Z)
    d_buy_eth: float     # ETH obtainable buying up to p(1+Z)
    d_z_usd: float       # d_sell_usd + p * d_buy_eth
    int_l: float         # sum of L x (tick width) over the +/-Z band (unit-free secondary)


def amount0(liq: float, sqrt_a: float, sqrt_b: float) -> float:
    return liq * (1.0 / sqrt_a - 1.0 / sqrt_b)


def amount1(liq: float, sqrt_a: float, sqrt_b: float) -> float:
    return liq * (sqrt_b - sqrt_a)


def _segments(state: PoolState, s_lo: float, s_hi: float) -> List[Tuple[float, float, int, float]]:
    """Pieces (sqrt_a, sqrt_b, L, tick_width) covering [s_lo, s_hi] with constant L on each piece."""
    t_lo, t_hi = tick_at_sqrt_ratio(s_lo), tick_at_sqrt_ratio(s_hi)
    if not (state.in_band(t_lo) and state.in_band(t_hi)):
        raise ReconstructionError(f"Z band [{t_lo}, {t_hi}] not inside the read band [{state.band_lo_tick}, {state.band_hi_tick}]")
    boundaries = [t for t in state.initialised_ticks(t_lo + 1, t_hi) ]  # ticks strictly above t_lo and <= t_hi
    pieces = []
    cur_s, cur_t = s_lo, t_lo
    for b in boundaries:
        sb = sqrt_ratio_at_tick(b)
        if sb <= cur_s:
            continue
        if sb > s_hi:
            break
        pieces.append((cur_s, sb, state.active_liquidity_at(cur_t), _tick_width(cur_s, sb)))
        cur_s, cur_t = sb, b
    if s_hi > cur_s:
        pieces.append((cur_s, s_hi, state.active_liquidity_at(cur_t), _tick_width(cur_s, s_hi)))
    return pieces


def _tick_width(s_a: float, s_b: float) -> float:
    import math
    return 2.0 * (math.log(s_b) - math.log(s_a)) / math.log(1.0001)


def depth(state: PoolState, z: float = Z_PRIMARY) -> Depth:
    if state.band_dirty or state.band_read_failed:
        raise ReconstructionError("depth requested on a band state that is dirty or whose re-read failed (6.1): "
                                  "sample only after the block-end re-read")
    pool: PoolSpec = state.pool
    s0 = state.sqrt_ratio
    p = state.price
    usdc_scale, weth_scale = 10.0 ** -USDC_DECIMALS, 10.0 ** -WETH_DECIMALS
    if pool.token0_is_weth:
        # p ~ raw price. Selling ETH (token0) pushes raw price DOWN to s0*sqrt(1-Z); USDC is token1 (amount1).
        s_sell = s0 * (1.0 - z) ** 0.5
        s_buy = s0 * (1.0 + z) ** 0.5
        sell_pieces = _segments(state, s_sell, s0)
        buy_pieces = _segments(state, s0, s_buy)
        d_sell = sum(amount1(L, a, b) for a, b, L, _ in sell_pieces) * usdc_scale
        d_buy = sum(amount0(L, a, b) for a, b, L, _ in buy_pieces) * weth_scale
    else:
        # p ~ 1/raw price. Selling ETH (token1) pushes raw price UP to s0/sqrt(1-Z); USDC is token0 (amount0).
        s_sell = s0 / (1.0 - z) ** 0.5
        s_buy = s0 / (1.0 + z) ** 0.5
        sell_pieces = _segments(state, s0, s_sell)
        buy_pieces = _segments(state, s_buy, s0)
        d_sell = sum(amount0(L, a, b) for a, b, L, _ in sell_pieces) * usdc_scale
        d_buy = sum(amount1(L, a, b) for a, b, L, _ in buy_pieces) * weth_scale
    int_l = sum(L * w for _, _, L, w in sell_pieces) + sum(L * w for _, _, L, w in buy_pieces)
    return Depth(z=z, p=p, d_sell_usd=d_sell, d_buy_eth=d_buy, d_z_usd=d_sell + p * d_buy, int_l=int_l)
