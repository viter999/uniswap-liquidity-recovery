"""Minute clock and the end-of-minute depth sampler (prereg v0.3 sections 5.1 and 6.2), shared by the calibration driver
and the confirmatory drivers so that the sampling convention is one piece of frozen code.

Convention (6.2): the state used for minute m is the state after the last log-bearing block (Mint/Burn/Swap of that
pool) with timestamp < 60*(m+1). Blocks are replayed in order; BEFORE applying a block B in minute mB the sampler records,
for every not-yet-sampled minute m with m <= mB-1, the depth of the current (pre-B) state, which is exactly the
end-of-minute state for those minutes. finish() fills the tail of the window with the final state.

A sample record is [D_0.005, D_0.01, D_0.02, int_L at Z=0.01, p, block]; a secondary Z that does not fit the read band
is None (D7), the primary Z must succeed.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

from .depth import depth
from .reconstruct import PoolState, ReconstructionError

ZS = (0.005, 0.01, 0.02)   # depth bands sampled: secondary, PRIMARY, secondary (6.2)
Z_PRIMARY_INDEX = 1
REC_INT_L, REC_P, REC_BLOCK = 3, 4, 5


class Clock:
    """Block -> timestamp for the log-bearing blocks of a chain (headers, the log field or the OP-stack formula, 6.6)."""

    def __init__(self, blocks: np.ndarray, timestamps: np.ndarray):
        blocks, timestamps = np.asarray(blocks, dtype=np.int64), np.asarray(timestamps, dtype=np.int64)
        if len(blocks) != len(timestamps) or np.any(np.diff(blocks) <= 0) or np.any(np.diff(timestamps) < 0):
            raise ValueError("Clock needs strictly increasing blocks and non-decreasing timestamps")
        self.blocks, self.ts = blocks, timestamps

    def timestamp(self, block: int) -> int:
        i = np.searchsorted(self.blocks, block)
        if i >= len(self.blocks) or self.blocks[i] != block:
            raise KeyError(block)
        return int(self.ts[i])

    def minute(self, block: int) -> int:
        return self.timestamp(block) // 60

    def last_block_before_minute(self, minute: int) -> int:
        """Last known (log-bearing) block with timestamp < 60*minute."""
        i = np.searchsorted(self.ts, 60 * minute, side="left") - 1
        if i < 0:
            raise KeyError(f"no block before minute {minute}")
        return int(self.blocks[i])

    def last_block_in_minute(self, minute: int) -> int:
        """Last known block with timestamp < 60*(minute+1), i.e. B(m) restricted to log-bearing blocks."""
        return self.last_block_before_minute(minute + 1)


def sample_depths(state: PoolState, block: int, zs: Sequence[float] = ZS) -> List[Optional[float]]:
    """One sample record for the state after `block` (see module docstring)."""
    out: List[Optional[float]] = []
    int_l: Optional[float] = None
    for i, z in enumerate(zs):
        try:
            d = depth(state, z)
        except ReconstructionError:
            if i == Z_PRIMARY_INDEX:
                raise
            out.append(None)
            continue
        out.append(d.d_z_usd)
        if i == Z_PRIMARY_INDEX:
            int_l = d.int_l
    return out + [int_l, state.price, block]


class WindowSampler:
    """Collects per-minute depth for one pool-episode window [w_start, w_end] as blocks are replayed in order."""

    def __init__(self, w_start: int, w_end: int, clock: Clock, seed_block: int = 0):
        self.w_start, self.w_end, self.clock = w_start, w_end, clock
        self.series: Dict[int, List[Optional[float]]] = {}
        self.last = w_start - 1
        self.prev_block = seed_block
        self.cached: Optional[List[Optional[float]]] = None

    @property
    def complete(self) -> bool:
        return self.last >= self.w_end

    def before_block(self, block: int, state: PoolState) -> None:
        """Call for EVERY block before it is applied (the block tag of a sample is the last block applied before it)."""
        if not self.complete:
            mb = self.clock.minute(block)
            hi = min(mb - 1, self.w_end)
            lo = max(self.last + 1, self.w_start)
            if hi >= lo:
                if self.cached is None:
                    self.cached = sample_depths(state, self.prev_block)
                for m in range(lo, hi + 1):
                    self.series[m] = self.cached
                self.last = hi
        self.cached = None   # the state is about to change
        self.prev_block = block

    def finish(self, state: PoolState) -> None:
        if not self.complete:
            d = sample_depths(state, self.prev_block)
            for m in range(max(self.last + 1, self.w_start), self.w_end + 1):
                self.series[m] = d
            self.last = self.w_end

    def series_for_z(self, z_index: int) -> Dict[int, Optional[float]]:
        return {m: v[z_index] for m, v in self.series.items()}

    def primary_series(self) -> Dict[int, float]:
        return {m: v[Z_PRIMARY_INDEX] for m, v in self.series.items()}

    def arrays(self) -> Dict[str, np.ndarray]:
        """Columns for the open dataset (deliverable 1): minute, D_0.005, D_0.01, D_0.02, int_L, p, block."""
        ms = sorted(self.series)
        cols = {"minute": np.array(ms, dtype=np.int64)}
        names = ["D_0.005", "D_0.01", "D_0.02", "int_L", "p"]
        for j, name in enumerate(names):
            cols[name] = np.array([float("nan") if self.series[m][j] is None else self.series[m][j] for m in ms], dtype=float)
        cols["block"] = np.array([self.series[m][REC_BLOCK] for m in ms], dtype=np.int64)
        return cols
