"""Archive band reads for the R2 route (prereg v0.3 section 6.1): slot0, liquidity, tickBitmap words covering the +/-8%
band around the slot0 price, then ticks() for every initialised tick in the band. Batched JSON-RPC eth_call at one block
tag (state-equivalent to a Multicall3 aggregate at that block; D5 addendum). Batch size is an endpoint property.
"""
from __future__ import annotations

from typing import Callable, Dict, List

from .common import PoolSpec, parse_int24, parse_int256
from .pull_logs import RpcError, Transport
from .reconstruct import BandSnapshot, band_tick_bounds

SEL_SLOT0 = "0x3850c7bd"     # slot0()
SEL_LIQ = "0x1a686502"       # liquidity()
SEL_BITMAP = "0x5339c296"    # tickBitmap(int16)
SEL_TICKS = "0xf30dba93"     # ticks(int24)

Retry = Callable[[Callable[[], object], str], object]


def enc_int(v: int) -> str:
    """ABI-encode a signed integer as a 32-byte two's-complement word (hex, no 0x)."""
    return format(v % (1 << 256), "064x")


def _plain_retry(fn, what):
    return fn()


def archive_calls(transport: Transport, pool: PoolSpec, datas: List[str], tag: str, batch: int, what: str,
                  retry: Retry = _plain_retry) -> List[str]:
    """eth_call for each calldata at the block tag, in batches of `batch`; returns result hex strings in order."""
    out: List[str] = []
    for i in range(0, len(datas), batch):
        part = datas[i:i + batch]
        res = retry(lambda: transport.batch([("eth_call", [{"to": pool.address, "data": d}, tag]) for d in part]),
                    f"{what} {pool.key} {tag}")
        out.extend(res)
    return out


def band_read(transport: Transport, pool: PoolSpec, block: int, batch: int, retry: Retry = _plain_retry) -> BandSnapshot:
    tag = hex(block)
    res = archive_calls(transport, pool, [SEL_SLOT0, SEL_LIQ], tag, batch, "slot0", retry)
    if not res[0] or len(res[0]) < 130 or not res[1] or len(res[1]) < 66:
        raise RpcError(f"empty slot0/liquidity at block {block} for {pool.key} (pool absent or endpoint without state)")
    s0 = res[0][2:]
    sqrt_p, tick = int(s0[:64], 16), parse_int24(s0[64:128])
    liq = int(res[1][2:66], 16)
    lo_t, hi_t = band_tick_bounds(sqrt_p, pool)
    lo_w, hi_w = (lo_t // pool.spacing) >> 8, (hi_t // pool.spacing) >> 8
    words = list(range(lo_w, hi_w + 1))
    bm = archive_calls(transport, pool, [SEL_BITMAP + enc_int(w) for w in words], tag, batch, "tickBitmap", retry)
    ticks: List[int] = []
    for w, r in zip(words, bm):
        bits = int(r[2:66], 16)
        for b in range(256):
            if bits >> b & 1:
                t = ((w << 8) + b) * pool.spacing
                if lo_t <= t <= hi_t:
                    ticks.append(t)
    tr = archive_calls(transport, pool, [SEL_TICKS + enc_int(t) for t in ticks], tag, batch, "ticks", retry)
    nets: Dict[int, int] = {t: parse_int256(r[2:][64:128]) for t, r in zip(ticks, tr)}
    return BandSnapshot(block, sqrt_p, tick, liq, nets, lo_t, hi_t)
