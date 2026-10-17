"""Block location and block timestamps (prereg v0.3 section 6.6, deviation D9).

- Ethereum and Arbitrum: B(m) = the last canonical block with timestamp < 60*(m+1), by bisection over headers; every header
  read lands in a HeaderCache so later lookups start from tight bounds (<= 30 reads per lookup, usually far fewer).
- Base and Unichain: the verified formula (common.block_at_minute_end) unless the chain was switched to headers.
- Ethereum log timestamps come from the `blockTimestamp` field of eth_getLogs (D9) and are cross-checked against
  eth_getBlockByNumber on a random sample (1%, at least 200 per window); any mismatch stops the pull.
"""
from __future__ import annotations

import bisect
import json
import math
import random
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .common import CHAIN_TIME, MINUTE, block_at_minute_end
from .pull_logs import RpcError, Transport, fetch_headers

SEED = 20261017
CROSS_CHECK_FRACTION = 0.01
CROSS_CHECK_MINIMUM = 200


class HeaderCache:
    """Known (block -> timestamp) pairs of one chain, kept sorted by block; timestamps are non-decreasing in block order."""

    def __init__(self):
        self.blocks: List[int] = []
        self.ts: List[int] = []
        self.reads = 0

    def __len__(self) -> int:
        return len(self.blocks)

    def get(self, n: int) -> Optional[int]:
        i = bisect.bisect_left(self.blocks, n)
        return self.ts[i] if i < len(self.blocks) and self.blocks[i] == n else None

    def add(self, n: int, ts: int) -> None:
        i = bisect.bisect_left(self.blocks, n)
        if i < len(self.blocks) and self.blocks[i] == n:
            if self.ts[i] != ts:
                raise RpcError(f"header cache conflict at block {n}: {self.ts[i]} vs {ts}")
            return
        self.blocks.insert(i, n)
        self.ts.insert(i, ts)

    def add_many(self, pairs: Iterable[Tuple[int, int]]) -> None:
        for n, t in pairs:
            self.add(n, t)

    def bounds_for_ts(self, limit: int) -> Tuple[Optional[int], Optional[int]]:
        """(largest known block with ts < limit, smallest known block with ts >= limit)."""
        i = bisect.bisect_left(self.ts, limit)
        lo = self.blocks[i - 1] if i > 0 else None
        hi = self.blocks[i] if i < len(self.blocks) else None
        return lo, hi

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"blocks": self.blocks, "ts": self.ts}), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "HeaderCache":
        c = cls()
        if path.is_file():
            d = json.loads(path.read_text(encoding="utf-8"))
            c.blocks, c.ts = list(d["blocks"]), list(d["ts"])
        return c


def header_ts(transport: Transport, n: int, cache: HeaderCache) -> int:
    t = cache.get(n)
    if t is not None:
        return t
    blk = transport.call("eth_getBlockByNumber", [hex(n), False])
    if blk is None:
        raise RpcError(f"header {n} missing")
    t = int(blk["timestamp"], 16)
    cache.add(n, t)
    cache.reads += 1
    return t


def last_block_before_ts(transport: Transport, limit: int, cache: HeaderCache, lo: int, hi: int) -> int:
    """Largest block n with timestamp(n) < limit, given ts(lo) < limit <= ts(hi). Bisection; every read is cached."""
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if header_ts(transport, mid, cache) < limit:
            lo = mid
        else:
            hi = mid
    return lo


def bracket(transport: Transport, limit: int, cache: HeaderCache, lo_guess: int, hi_guess: int) -> Tuple[int, int]:
    """Known bounds around `limit` from the cache, widened from the guesses when the cache has none."""
    lo, hi = cache.bounds_for_ts(limit)
    if lo is None:
        lo = max(0, lo_guess)
        while lo > 0 and header_ts(transport, lo, cache) >= limit:
            lo //= 2
        if header_ts(transport, lo, cache) >= limit:
            raise RpcError(f"no block before timestamp {limit}")
    if hi is None:
        hi = max(hi_guess, lo + 1)
        if header_ts(transport, hi, cache) < limit:
            raise RpcError(f"block {hi} is still before timestamp {limit}; cutoff too early")
    return lo, hi


class ArrayIndex:
    """Read-only (block, timestamp) arrays (e.g. every log-bearing block of the clock series) giving tight bisection
    bounds without holding millions of Python ints."""

    def __init__(self, blocks, ts):
        import numpy as np
        self.blocks, self.ts = np.asarray(blocks, dtype=np.int64), np.asarray(ts, dtype=np.int64)

    def bounds_for_ts(self, limit: int) -> Tuple[Optional[int], Optional[int]]:
        import numpy as np
        i = int(np.searchsorted(self.ts, limit, side="left"))
        lo = int(self.blocks[i - 1]) if i > 0 else None
        hi = int(self.blocks[i]) if i < len(self.blocks) else None
        return lo, hi


class BlockLocator:
    """B(m) for one chain: formula on OP-stack chains (unless switched), header bisection elsewhere.

    `index` (optional, bounds_for_ts) narrows the bracket before any header read; `cache` holds every header read."""

    def __init__(self, chain: str, mode: str, transport: Optional[Transport], cache: HeaderCache,
                 lo_guess: int, hi_guess: int, index=None):
        if mode == "formula" and chain not in CHAIN_TIME:
            raise ValueError(f"{chain} has no timestamp formula")
        self.chain, self.mode, self.transport, self.cache = chain, mode, transport, cache
        self.lo_guess, self.hi_guess, self.index = lo_guess, hi_guess, index

    def last_block_before(self, limit_ts: int) -> int:
        if self.mode == "formula":
            genesis, bt = CHAIN_TIME[self.chain]
            return (limit_ts - 1 - genesis) // bt
        lo, hi = bracket(self.transport, limit_ts, self.cache, self.lo_guess, self.hi_guess)
        if self.index is not None:
            ilo, ihi = self.index.bounds_for_ts(limit_ts)
            if ilo is not None and ilo > lo:
                lo = ilo
            if ihi is not None and ihi < hi:
                hi = ihi
        return last_block_before_ts(self.transport, limit_ts, self.cache, lo, hi)

    def block_at_minute_end(self, minute: int) -> int:
        """B(m): last block with timestamp < 60*(m+1)."""
        if self.mode == "formula":
            return block_at_minute_end(self.chain, minute)
        return self.last_block_before(MINUTE * (minute + 1))

    def first_block_at_or_after(self, ts: int) -> int:
        return self.last_block_before(ts) + 1


# ----------------------------------------------------------------------------- Ethereum log-field timestamps (D9)
def timestamps_from_logs(raw_logs: Iterable[dict]) -> Dict[int, int]:
    """{block: timestamp} from the `blockTimestamp` field; missing or inconsistent fields are a hard failure."""
    out: Dict[int, int] = {}
    for lg in raw_logs:
        n = int(lg["blockNumber"], 16)
        v = lg.get("blockTimestamp")
        if v is None:
            raise RpcError(f"log at block {n} carries no blockTimestamp field")
        t = int(v, 16)
        if out.get(n, t) != t:
            raise RpcError(f"inconsistent blockTimestamp inside block {n}: {out[n]} vs {t}")
        out[n] = t
    return out


def cross_check_timestamps(transport: Transport, ts_map: Dict[int, int], batch: int, fraction: float = CROSS_CHECK_FRACTION,
                           minimum: int = CROSS_CHECK_MINIMUM, seed: int = SEED, cache: Optional[HeaderCache] = None) -> dict:
    """Compare a random sample of the log-field timestamps with eth_getBlockByNumber (ruling 17 Oct 2026, item 3)."""
    blocks = sorted(ts_map)
    k = min(len(blocks), max(minimum, math.ceil(fraction * len(blocks))))
    sample = sorted(random.Random(seed).sample(blocks, k)) if k < len(blocks) else blocks
    headers = fetch_headers(transport, sample, batch_size=batch)
    if cache is not None:
        cache.add_many((n, h["timestamp"]) for n, h in headers.items())
    mism = [(n, ts_map[n], headers[n]["timestamp"]) for n in sample if headers[n]["timestamp"] != ts_map[n]]
    return {"log_bearing_blocks": len(blocks), "sampled": len(sample), "mismatches": mism, "ok": not mism}


def window_block_range(locator: BlockLocator, window_start_min: int, window_end_min: int) -> Tuple[int, int]:
    """(b0, b_end): b0 = last block with timestamp < 60*window_start (5.3); b_end = B(window_end)."""
    b0 = locator.block_at_minute_end(window_start_min - 1)
    b_end = locator.block_at_minute_end(window_end_min)
    return b0, b_end


def sample_blocks_in_windows(windows: Iterable[Tuple[int, int]], n: int, seed: int = SEED) -> List[int]:
    """n distinct random blocks from the union of inclusive block ranges (6.6 and 8.4: 'inside episode windows')."""
    merged: List[List[int]] = []
    for a, b in sorted((int(a), int(b)) for a, b in windows):
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    total = sum(b - a + 1 for a, b in merged)
    rng = random.Random(seed)
    picks = set()
    k = min(n, total)
    while len(picks) < k:
        r = rng.randrange(total)
        for a, b in merged:
            span = b - a + 1
            if r < span:
                picks.add(a + r)
                break
            r -= span
    return sorted(picks)


def verify_formula_on(transport: Transport, chain: str, blocks: List[int], batch: int) -> dict:
    """Header timestamp must equal the formula exactly on every listed block (6.6)."""
    from .common import timestamp_from_block
    headers: Dict[int, dict] = {}
    for i in range(0, len(blocks), batch):
        headers.update(fetch_headers(transport, blocks[i:i + batch], batch_size=batch))
    mism = [(b, headers[b]["timestamp"], timestamp_from_block(chain, b)) for b in blocks
            if headers[b]["timestamp"] != timestamp_from_block(chain, b)]
    return {"chain": chain, "sampled": len(blocks), "mismatches": mism, "ok": not mism}
