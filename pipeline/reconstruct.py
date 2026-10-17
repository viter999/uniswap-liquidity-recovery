"""Pool liquidity-state reconstruction (prereg v0.3 section 6.1).

R2: snapshot of the tick band at b0 (slot0, liquidity, initialised ticks' liquidityNet within +/-8% of the slot0 price)
    followed by replay of the window's Mint/Burn/Swap logs; band re-read when the end-of-block price leaves +/-6%.
R1: full replay from pool initialisation (Initialize -> every Mint/Burn/Swap), used as the cross-check.

Active liquidity convention (Uniswap v3): a position [lower, upper) is active when lower <= tick < upper; crossing an
initialised tick i upward adds liquidityNet[i]. Hence for the current tick c with active liquidity L_c:
    L(t) = L_c + sum_{c < i <= t} net[i]      for t > c
    L(t) = L_c - sum_{t < i <= c} net[i]      for t < c
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .common import (PoolSpec, Q96, price_usd_per_eth, sqrt_ratio_at_tick, tick_at_sqrt_ratio,
                     floor_to_spacing, ceil_to_spacing)

BAND_READ = 0.08       # snapshot band half-width in p-space (6.1)
BAND_REREAD = 0.06     # re-read trigger half-width in p-space (6.1)
BAND_Z_MAX = 0.02      # largest depth band (secondary Z) that must fit inside the read band at every sample (D7)
REREAD_ATTEMPTS = 3


class ReconstructionError(RuntimeError):
    """Hard failure: Swap-event liquidity mismatch, band violation, or malformed event order."""


@dataclass
class Event:
    """One pool log. kind in {"mint", "burn", "swap", "initialize"}. Amounts/ticks are raw ints."""
    block: int
    log_index: int
    kind: str
    tick_lower: int = 0
    tick_upper: int = 0
    amount: int = 0                 # liquidity delta for mint/burn
    sqrt_price_x96: int = 0         # swap / initialize
    liquidity: int = 0              # swap event's liquidity field (active after the swap)
    tick: int = 0                   # swap / initialize

    @property
    def order(self) -> Tuple[int, int]:
        return self.block, self.log_index


@dataclass
class BandSnapshot:
    """What a Multicall3 band read returns (6.1): slot0, active liquidity, and liquidityNet of initialised ticks in the band."""
    block: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    liquidity_net: Dict[int, int]   # initialised tick -> net
    band_lo_tick: int               # inclusive bounds of the ticks that were read (multiples of spacing)
    band_hi_tick: int


@dataclass
class PoolState:
    pool: PoolSpec
    sqrt_price_x96: int
    tick: int
    liquidity: int
    liquidity_net: Dict[int, int] = field(default_factory=dict)
    band_lo_tick: Optional[int] = None      # None = full map (R1)
    band_hi_tick: Optional[int] = None
    band_center_p: Optional[float] = None   # p at the band centre (slot0 price at the last read)
    band_reread_count: int = 0
    band_read_failed: bool = False
    rereads: List[dict] = field(default_factory=list)
    band_dirty: bool = False        # a swap left the read band since the last read: nets crossed outside are unknown
    swaps_checked: int = 0          # swaps whose liquidity field was verified against the reconstruction
    swaps_unchecked: int = 0        # swaps adopted without the check (band exit, or same-block followers after one)
    _sorted: List[int] = field(default_factory=list, repr=False)   # initialised ticks, ascending (index over liquidity_net)

    def __post_init__(self) -> None:
        self._sorted = sorted(self.liquidity_net)

    def _sync(self) -> None:
        """Rebuild the index if liquidity_net was edited directly (tests do this); O(1) when in step."""
        if len(self._sorted) != len(self.liquidity_net):
            self._sorted = sorted(self.liquidity_net)

    # ------------------------------------------------------------------ queries
    @property
    def price(self) -> float:
        return price_usd_per_eth(self.sqrt_price_x96, self.pool)

    @property
    def sqrt_ratio(self) -> float:
        return self.sqrt_price_x96 / Q96

    def in_band(self, tick: int) -> bool:
        if self.band_lo_tick is None:
            return True
        return self.band_lo_tick <= tick <= self.band_hi_tick

    def active_liquidity_at(self, tick: int) -> int:
        """L(tick) from the current tick and the known liquidityNet map (see module docstring)."""
        if not self.in_band(tick):
            raise ReconstructionError(f"tick {tick} outside known band [{self.band_lo_tick}, {self.band_hi_tick}]")
        self._sync()
        c, lq = self.tick, self.liquidity
        s, net = self._sorted, self.liquidity_net
        if tick > c:
            # ticks i with c < i <= tick
            for i in s[bisect.bisect_right(s, c):bisect.bisect_right(s, tick)]:
                lq += net[i]
        elif tick < c:
            # ticks i with tick < i <= c
            for i in s[bisect.bisect_right(s, tick):bisect.bisect_right(s, c)]:
                lq -= net[i]
        return lq

    def initialised_ticks(self, lo: int, hi: int) -> List[int]:
        self._sync()
        s = self._sorted
        return [i for i in s[bisect.bisect_left(s, lo):bisect.bisect_right(s, hi)] if self.liquidity_net[i] != 0]

    def _set_net(self, t: int, value: int) -> None:
        """Keep liquidity_net and the sorted index in step; a zero entry is removed (uninitialised)."""
        if value == 0:
            if t in self.liquidity_net:
                del self.liquidity_net[t]
                i = bisect.bisect_left(self._sorted, t)
                if i < len(self._sorted) and self._sorted[i] == t:
                    del self._sorted[i]
            return
        if t not in self.liquidity_net:
            bisect.insort(self._sorted, t)
        self.liquidity_net[t] = value

    # ------------------------------------------------------------------ event application
    def apply(self, ev: Event) -> None:
        if ev.kind == "mint":
            self._modify(ev.tick_lower, ev.tick_upper, ev.amount)
        elif ev.kind == "burn":
            self._modify(ev.tick_lower, ev.tick_upper, -ev.amount)
        elif ev.kind == "swap":
            self._swap(ev)
        elif ev.kind == "initialize":
            self.sqrt_price_x96, self.tick = ev.sqrt_price_x96, ev.tick
        else:
            raise ReconstructionError(f"unknown event kind {ev.kind}")

    def _modify(self, lower: int, upper: int, delta: int) -> None:
        if lower >= upper or lower % self.pool.spacing or upper % self.pool.spacing:
            raise ReconstructionError(f"bad position bounds {lower},{upper} for spacing {self.pool.spacing}")
        for t, sign in ((lower, +1), (upper, -1)):
            if self.in_band(t):
                self._set_net(t, self.liquidity_net.get(t, 0) + sign * delta)
        if lower <= self.tick < upper:
            self.liquidity += delta
            if self.liquidity < 0:
                raise ReconstructionError("negative active liquidity after position change")

    def _swap(self, ev: Event) -> None:
        new_tick = ev.tick
        # The walk from the current tick to the new one is only valid if both ends lie inside the read band: ticks crossed
        # outside the band are unknown to an R2 state. After a same-block exit the returning swap adopts the event's
        # liquidity (D6); from then on in-band walks are checkable again (R15), while `band_dirty` still forces the
        # block-end re-read.
        if self.in_band(new_tick) and self.in_band(self.tick):
            expected = self.active_liquidity_at(new_tick)
            if expected != ev.liquidity:
                raise ReconstructionError(
                    f"Swap liquidity mismatch at block {ev.block} log {ev.log_index}: reconstructed {expected}, event {ev.liquidity}")
            self.liquidity = expected
            self.swaps_checked += 1
        else:
            # price left (or is returning to) the known band inside a block: adopt the event's value and force a
            # re-read at the end of this block (needs_reread) before any depth sample is taken
            self.liquidity = ev.liquidity
            self.swaps_unchecked += 1
            if self.band_lo_tick is not None:
                self.band_dirty = True
        self.sqrt_price_x96, self.tick = ev.sqrt_price_x96, new_tick

    # ------------------------------------------------------------------ band bookkeeping (R2)
    def z_band_inside(self, z: float = BAND_Z_MAX) -> bool:
        """True if the +/-z depth band around the current price lies inside the read band (R1 full map: always)."""
        if self.band_lo_tick is None:
            return True
        lo, hi = band_tick_bounds(self.sqrt_price_x96, self.pool, half_width=z)
        return self.band_lo_tick <= lo and hi <= self.band_hi_tick

    def reread_reason(self) -> Optional[str]:
        """Why a re-read is due at the end of this block, or None (6.1 plus D6 'dirty' and D7 'z_edge')."""
        if self.band_center_p is None:
            return None
        if self.band_dirty:
            return "dirty"
        if not self.in_band(self.tick):
            return "exit"
        if abs(self.price / self.band_center_p - 1.0) > BAND_REREAD:
            return "drift"
        if not self.z_band_inside(BAND_Z_MAX):
            return "z_edge"
        return None

    def needs_reread(self) -> bool:
        return self.reread_reason() is not None

    def load_snapshot(self, snap: BandSnapshot) -> None:
        self.sqrt_price_x96, self.tick, self.liquidity = snap.sqrt_price_x96, snap.tick, snap.liquidity
        self.liquidity_net = {t: n for t, n in snap.liquidity_net.items() if n != 0}
        self._sorted = sorted(self.liquidity_net)
        self.band_lo_tick, self.band_hi_tick = snap.band_lo_tick, snap.band_hi_tick
        self.band_center_p = self.price
        self.band_dirty = False


# ----------------------------------------------------------------------------- band geometry (6.1)
def band_tick_bounds(sqrt_price_x96: int, pool: PoolSpec, half_width: float = BAND_READ) -> Tuple[int, int]:
    """Ticks covering p*(1 -/+ half_width), widened outward to multiples of the spacing.

    In p-space the band is [p(1-w), p(1+w)]; in raw sqrt units that is s*sqrt(1-w)..s*sqrt(1+w) for token0 = WETH and
    s/sqrt(1+w)..s/sqrt(1-w) for token0 = USDC (prereg 6.2 bounds, applied to the read band)."""
    s = sqrt_price_x96 / Q96
    if pool.token0_is_weth:
        lo_s, hi_s = s * (1.0 - half_width) ** 0.5, s * (1.0 + half_width) ** 0.5
    else:
        lo_s, hi_s = s / (1.0 + half_width) ** 0.5, s / (1.0 - half_width) ** 0.5
    lo_t = floor_to_spacing(tick_at_sqrt_ratio(lo_s), pool.spacing)
    hi_t = ceil_to_spacing(tick_at_sqrt_ratio(hi_s) + 1, pool.spacing)
    return lo_t, hi_t


SnapshotReader = Callable[[int], BandSnapshot]  # block -> snapshot (archive multicall in production; fake in tests)


def state_from_snapshot(pool: PoolSpec, snap: BandSnapshot) -> PoolState:
    st = PoolState(pool, snap.sqrt_price_x96, snap.tick, snap.liquidity)
    st.load_snapshot(snap)
    return st


def net_map_from_events(events: Iterable[Event], pool: PoolSpec, up_to_block: int) -> Dict[int, int]:
    """liquidityNet per tick from every Mint/Burn with block <= up_to_block (R1: full Mint/Burn replay since creation)."""
    nets: Dict[int, int] = {}
    for ev in events:
        if ev.block > up_to_block or ev.kind not in ("mint", "burn"):
            continue
        if ev.tick_lower >= ev.tick_upper or ev.tick_lower % pool.spacing or ev.tick_upper % pool.spacing:
            raise ReconstructionError(f"bad position bounds {ev.tick_lower},{ev.tick_upper} at block {ev.block}")
        delta = ev.amount if ev.kind == "mint" else -ev.amount
        nets[ev.tick_lower] = nets.get(ev.tick_lower, 0) + delta
        nets[ev.tick_upper] = nets.get(ev.tick_upper, 0) - delta
    return {t: n for t, n in nets.items() if n != 0}


def state_from_net_map(pool: PoolSpec, nets: Dict[int, int], sqrt_price_x96: int, tick: int) -> PoolState:
    """R1 state at a block: the lifetime net map plus slot0 at that block; active L = sum of net over ticks <= tick."""
    liq = sum(n for t, n in nets.items() if t <= tick)
    if liq < 0:
        raise ReconstructionError("negative active liquidity from the lifetime net map")
    return PoolState(pool, sqrt_price_x96, tick, liq, dict(nets))


def state_from_full_replay(pool: PoolSpec, events: Iterable[Event]) -> PoolState:
    """R1: Initialize then every Mint/Burn/Swap in (block, log_index) order; unbounded map."""
    evs = sorted(events, key=lambda e: e.order)
    if not evs or evs[0].kind != "initialize":
        raise ReconstructionError("R1 replay must start with the Initialize event")
    st = PoolState(pool, evs[0].sqrt_price_x96, evs[0].tick, 0)
    for ev in evs[1:]:
        st.apply(ev)
    return st


def replay_window(state: PoolState, events: Iterable[Event], reader: Optional[SnapshotReader],
                  on_block_end: Optional[Callable[[int, PoolState], None]] = None) -> PoolState:
    """Apply window events block by block; after each block run the re-read rule (R2) and the caller's sampler.

    reader(block) must return the band around the price at the END of that block. A failing reader (exception or None)
    is retried up to REREAD_ATTEMPTS times; then band_read_failed is set and replay stops (pool-episode dropped, 7.1a)."""
    evs = sorted(events, key=lambda e: e.order)
    i = 0
    while i < len(evs):
        blk = evs[i].block
        while i < len(evs) and evs[i].block == blk:
            state.apply(evs[i])
            i += 1
        reason = state.reread_reason() if state.band_lo_tick is not None else None
        if reason is not None:
            if reader is None:
                raise ReconstructionError(f"band {reason} at block {blk} with no reader")
            old = state.band_center_p
            snap = None
            for _ in range(REREAD_ATTEMPTS):
                try:
                    snap = reader(blk)
                except Exception:  # noqa: BLE001 - counted as a failed attempt
                    snap = None
                if snap is not None:
                    break
            if snap is None:
                state.band_read_failed = True
                return state
            state.load_snapshot(snap)
            state.band_reread_count += 1
            state.rereads.append({"block": blk, "reason": reason, "old_center_p": old, "new_center_p": state.band_center_p})
        if on_block_end is not None:
            on_block_end(blk, state)
    return state
