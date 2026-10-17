"""Shared constants, pool table, price orientation, tick math and block timestamps (prereg v0.3 sections 3, 5.1, 6.6)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

Q96 = 2 ** 96
TICK_BASE = 1.0001
LN_TICK = math.log(TICK_BASE)
MIN_TICK = -887272
MAX_TICK = 887272
USDC_DECIMALS = 6
WETH_DECIMALS = 18
MINUTE = 60

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"


@dataclass(frozen=True)
class PoolSpec:
    """One study pool (prereg section 3). token0_is_weth fixes the orientation of section 5.1."""
    chain: str
    tier: str          # "0.05%" or "0.3%"
    fee: int           # 500 or 3000
    spacing: int       # 10 or 60
    address: str
    token0: str        # "USDC" or "WETH"
    token1: str
    role: str          # "confirmatory" | "exploratory"
    clock: bool = False

    @property
    def token0_is_weth(self) -> bool:
        return self.token0 == "WETH"

    @property
    def dec0(self) -> int:
        return WETH_DECIMALS if self.token0_is_weth else USDC_DECIMALS

    @property
    def dec1(self) -> int:
        return USDC_DECIMALS if self.token0_is_weth else WETH_DECIMALS

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.tier}"


POOLS: Dict[str, PoolSpec] = {
    "ethereum:0.05%": PoolSpec("ethereum", "0.05%", 500, 10, "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640", "USDC", "WETH", "confirmatory", clock=True),
    "ethereum:0.3%": PoolSpec("ethereum", "0.3%", 3000, 60, "0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8", "USDC", "WETH", "confirmatory"),
    "arbitrum:0.05%": PoolSpec("arbitrum", "0.05%", 500, 10, "0xC6962004f452bE9203591991D15f6b388e09E8D0", "WETH", "USDC", "confirmatory"),
    "arbitrum:0.3%": PoolSpec("arbitrum", "0.3%", 3000, 60, "0xC473e2aEE3441BF9240Be85eb122aBB059A3B57c", "WETH", "USDC", "confirmatory"),
    "base:0.05%": PoolSpec("base", "0.05%", 500, 10, "0xd0b53D9277642d899DF5C87A3966A349A798F224", "WETH", "USDC", "confirmatory"),
    "base:0.3%": PoolSpec("base", "0.3%", 3000, 60, "0x6c561B446416E1A00E8E93E221854d6eA4171372", "WETH", "USDC", "confirmatory"),
    "unichain:0.3%": PoolSpec("unichain", "0.3%", 3000, 60, "0x8927058918e3cff6f55efe45a58db1be1f069e49", "USDC", "WETH", "exploratory"),
}
CONFIRMATORY_CHAINS = ("ethereum", "arbitrum", "base")
CLOCK_POOL = POOLS["ethereum:0.05%"]

# OP-stack chains: timestamp(n) = genesis_timestamp + block_time * n (verified 2026-10-17, probes/probe_genesis_20261017.txt)
CHAIN_TIME: Dict[str, Tuple[int, int]] = {"base": (1686789347, 2), "unichain": (1730748359, 1)}


def confirmatory_pools() -> list:
    return [p for p in POOLS.values() if p.role == "confirmatory"]


# ----------------------------------------------------------------------------- price orientation (5.1)
def human_price_token1_per_token0(sqrt_price_x96: int, pool: PoolSpec) -> float:
    """P_h = (sqrtPriceX96 / 2^96)^2 * 10^(dec0 - dec1): price of token0 in token1, human units."""
    s = sqrt_price_x96 / Q96
    return s * s * 10.0 ** (pool.dec0 - pool.dec1)


def price_usd_per_eth(sqrt_price_x96: int, pool: PoolSpec) -> float:
    """p = P_h if token0 = WETH else 1 / P_h (prereg 5.1)."""
    ph = human_price_token1_per_token0(sqrt_price_x96, pool)
    return ph if pool.token0_is_weth else 1.0 / ph


def sqrt_price_x96_from_usd(p: float, pool: PoolSpec) -> int:
    """Inverse of price_usd_per_eth (used by synthetic tests)."""
    ph = p if pool.token0_is_weth else 1.0 / p
    raw = ph * 10.0 ** (pool.dec1 - pool.dec0)
    return int(round(math.sqrt(raw) * Q96))


# ----------------------------------------------------------------------------- tick math
def sqrt_ratio_at_tick(tick: int) -> float:
    """sqrt(1.0001^tick) as a float (raw units; adequate for the 0.1% depth tolerance)."""
    return math.exp(0.5 * tick * LN_TICK)


def tick_at_sqrt_ratio(sqrt_ratio: float) -> int:
    """floor(log_1.0001(sqrt^2)); the tick whose range contains the price."""
    t = math.floor(2.0 * math.log(sqrt_ratio) / LN_TICK)
    # guard against floating error at exact boundaries
    while sqrt_ratio_at_tick(t + 1) <= sqrt_ratio:
        t += 1
    while sqrt_ratio_at_tick(t) > sqrt_ratio:
        t -= 1
    return t


def floor_to_spacing(tick: int, spacing: int) -> int:
    return (tick // spacing) * spacing


def ceil_to_spacing(tick: int, spacing: int) -> int:
    return -((-tick) // spacing) * spacing


def bitmap_position(tick: int, spacing: int) -> Tuple[int, int]:
    """tickBitmap addressing: compressed = floor(tick / spacing); word = compressed >> 8; bit = compressed & 255."""
    compressed = tick // spacing
    return compressed >> 8, compressed & 255


# ----------------------------------------------------------------------------- time (5.1, 6.6)
def minute_of(timestamp: int) -> int:
    """Minute index m = floor(timestamp / 60), UTC."""
    return timestamp // MINUTE


def timestamp_from_block(chain: str, block_number: int) -> int:
    genesis, bt = CHAIN_TIME[chain]
    return genesis + bt * block_number


def block_at_minute_end(chain: str, minute: int) -> int:
    """B(m) = floor((60*(m+1) - 1 - genesis) / block_time): last block with timestamp < 60*(m+1) (OP-stack chains)."""
    genesis, bt = CHAIN_TIME[chain]
    return (MINUTE * (minute + 1) - 1 - genesis) // bt


def block_at_minute_end_from_headers(headers: Dict[int, int], minute: int) -> Optional[int]:
    """Same as block_at_minute_end but from an explicit {block_number: timestamp} map (Ethereum / Arbitrum)."""
    limit = MINUTE * (minute + 1)
    best = None
    for n, ts in headers.items():
        if ts < limit and (best is None or n > best):
            best = n
    return best


def parse_int24(word_hex: str) -> int:
    """int24 from the low 6 hex chars of a 32-byte word, two's complement."""
    v = int(word_hex[-6:], 16)
    return v - (1 << 24) if v >= (1 << 23) else v


def parse_int256(word_hex: str) -> int:
    v = int(word_hex, 16)
    return v - (1 << 256) if v >= (1 << 255) else v
