"""Synthetic builders shared by the tests (no real data anywhere)."""
from __future__ import annotations

import math
from typing import Dict, List

import numpy as np

from pipeline.common import POOLS, PoolSpec, Q96, sqrt_price_x96_from_usd, tick_at_sqrt_ratio
from pipeline.episodes import Episode, HORIZON_MIN
from pipeline.reconstruct import BandSnapshot, Event, PoolState, band_tick_bounds, state_from_snapshot

M0 = 28_401_120  # 2024-01-01 00:00 UTC in minutes since the epoch (28,401,120 * 60 = 1,704,067,200)


def flat_series(n: int, p: float = 3000.0) -> np.ndarray:
    return np.full(n, p, dtype=float)


def plant_move(prices: np.ndarray, at: int, frac: float, ramp: int = 10) -> None:
    """Multiply the price by (1+frac) over `ramp` minutes starting at index `at`, held afterwards."""
    base = prices[at - 1]
    for k in range(ramp):
        prices[at + k] = base * (1.0 + frac * (k + 1) / ramp)
    prices[at + ramp:] = base * (1.0 + frac)


def make_episode(eid: int, t1_first: int, t1_last: int, lag: int = 60, direction: str = "ETH_down") -> Episode:
    return Episode(eid, t1_first, t1_last, t1_first - lag, t1_last - t1_first + 1, 0.06,
                   -0.06 if direction == "ETH_down" else 0.06, direction, (t1_last - t1_first) > 2880)


def single_range_state(pool: PoolSpec, p_usd: float, liquidity: int, half_ticks: int = 20000) -> PoolState:
    """One position spanning the whole read band (liquidity constant); band read around p."""
    sp = sqrt_price_x96_from_usd(p_usd, pool)
    tick = tick_at_sqrt_ratio(sp / Q96)
    lo, hi = band_tick_bounds(sp, pool)
    lower = ((tick - half_ticks) // pool.spacing) * pool.spacing
    upper = ((tick + half_ticks) // pool.spacing) * pool.spacing
    assert lower <= lo and upper >= hi
    snap = BandSnapshot(block=100, sqrt_price_x96=sp, tick=tick, liquidity=liquidity,
                        liquidity_net={}, band_lo_tick=lo, band_hi_tick=hi)
    return state_from_snapshot(pool, snap)


def depth_series(ep: Episode, d_pre: float, dip_at: int, dip_val: float, recover_at: int,
                 extra: Dict[int, float] = None) -> Dict[int, float]:
    """Dense synthetic depth series over the episode's data window: d_pre before dip_at, dip_val until recover_at, then d_pre."""
    out = {}
    for m in range(ep.window_start, ep.window_end + 1):
        out[m] = d_pre if (m < dip_at or m >= recover_at) else dip_val
    if extra:
        out.update(extra)
    return out


# ----------------------------------------------------------------------------- synthetic chains and archives (drivers)
from typing import Callable, Iterable, Optional  # noqa: E402

from pipeline.common import parse_int256, timestamp_from_block  # noqa: E402
from pipeline.reconstruct import state_from_full_replay  # noqa: E402

SEL_SLOT0, SEL_LIQ, SEL_BITMAP, SEL_TICKS = "0x3850c7bd", "0x1a686502", "0x5339c296", "0xf30dba93"


def word(v: int) -> str:
    return format(v % (1 << 256), "064x")


class FormulaBlocks(dict):
    """Lazy {n: (timestamp, hash)} for an OP-stack chain: every block 0..latest exists and follows the 6.6 formula."""

    def __init__(self, chain: str, latest: int):
        super().__init__()
        self.chain, self.latest = chain, latest

    def __contains__(self, n) -> bool:
        return isinstance(n, int) and 0 <= n <= self.latest

    def __missing__(self, n):
        if not (0 <= n <= self.latest):
            raise KeyError(n)
        return timestamp_from_block(self.chain, n), "0x" + format(n, "064x")


class FakeArchive:
    """eth_call answers (slot0, liquidity, tickBitmap, ticks) at any block for one synthetic pool, from a full replay."""

    def __init__(self, pool: PoolSpec, events: Iterable[Event]):
        self.pool = pool
        self.events = sorted(events, key=lambda e: e.order)
        self._cache: Dict[int, PoolState] = {}

    def state_at(self, block: int) -> PoolState:
        if block not in self._cache:
            self._cache[block] = state_from_full_replay(self.pool, [e for e in self.events if e.block <= block])
        return self._cache[block]

    def __call__(self, to: str, data: str, block: int) -> str:
        if to.lower() != self.pool.address.lower() or not self.events or block < self.events[0].block:
            return "0x"
        sel, arg = data[:10].lower(), data[10:74]
        st = self.state_at(block)
        if sel == SEL_SLOT0:
            return "0x" + word(st.sqrt_price_x96) + word(st.tick) + word(0) * 5
        if sel == SEL_LIQ:
            return "0x" + word(st.liquidity)
        if sel == SEL_BITMAP:
            w = parse_int256(arg)
            bits = 0
            for t, n in st.liquidity_net.items():
                c = t // self.pool.spacing
                if n != 0 and (c >> 8) == w:
                    bits |= 1 << (c & 255)
            return "0x" + word(bits)
        if sel == SEL_TICKS:
            n = st.liquidity_net.get(parse_int256(arg), 0)
            return "0x" + word(abs(n)) + word(n) + word(0) * 6
        raise ValueError(f"unknown selector {sel}")


class SwapPlanner:
    """Builds consistent synthetic pool events: swaps carry the active liquidity the chain would report."""

    def __init__(self, pool: PoolSpec, init_block: int, p0: float, liquidity: int, half_ticks: int = 20000):
        sp = sqrt_price_x96_from_usd(p0, pool)
        tick = tick_at_sqrt_ratio(sp / Q96)
        self.pool = pool
        self.lower = ((tick - half_ticks) // pool.spacing) * pool.spacing
        self.upper = ((tick + half_ticks) // pool.spacing) * pool.spacing
        self.events: List[Event] = [Event(init_block, 0, "initialize", sqrt_price_x96=sp, tick=tick),
                                    Event(init_block + 1, 0, "mint", tick_lower=self.lower, tick_upper=self.upper, amount=liquidity)]
        self.state = state_from_full_replay(pool, self.events)
        self.idx: Dict[int, int] = {}

    def _next_index(self, block: int) -> int:
        self.idx[block] = self.idx.get(block, -1) + 1
        return self.idx[block]

    def swap_to(self, block: int, p: float) -> Event:
        sp = sqrt_price_x96_from_usd(p, self.pool)
        tick = tick_at_sqrt_ratio(sp / Q96)
        ev = Event(block, self._next_index(block), "swap", sqrt_price_x96=sp, liquidity=self.state.active_liquidity_at(tick), tick=tick)
        self.state.apply(ev)
        self.events.append(ev)
        return ev

    def modify(self, block: int, kind: str, amount: int, lower: Optional[int] = None, upper: Optional[int] = None) -> Event:
        ev = Event(block, self._next_index(block), kind, tick_lower=self.lower if lower is None else lower,
                   tick_upper=self.upper if upper is None else upper, amount=amount)
        self.state.apply(ev)
        self.events.append(ev)
        return ev


def shock_scenario(planner: SwapPlanner, block_at: Callable[[int], int], ep: Episode, p0: float, liquidity: int,
                   drop: float = 0.06, recover_after: int = 90, step_min: int = 10) -> None:
    """Swaps every step_min minutes over the window (price p0, falling by `drop` over the lag), a burn of half the
    liquidity in the swap block of minute t0 + step_min (the dip) and a mint restoring it in the swap block of minute
    t1_last + recover_after (so T_rec = recover_after). Modifications follow the swap inside the same block."""
    m_lo, m_hi = ep.window_start - 60, ep.window_end + 60
    burn_at, mint_at = ep.t0 + step_min, ep.t1_last + recover_after
    assert (burn_at - m_lo) % step_min == 0 and (mint_at - m_lo) % step_min == 0
    for m in range(m_lo, m_hi + 1, step_min):
        frac = min(1.0, max(0.0, (m - ep.t0) / (ep.t1_first - ep.t0)))
        p = p0 * (1.0 - drop * frac) * (1.0 + 0.0005 * ((m // step_min) % 3 - 1))
        planner.swap_to(block_at(m), p)
        if m == burn_at:
            planner.modify(block_at(m), "burn", liquidity // 2)
        if m == mint_at:
            planner.modify(block_at(m), "mint", liquidity // 2)
