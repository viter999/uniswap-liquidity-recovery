"""Per-chain data routes (prereg v0.3 sections 8.1, 8.2, 8.4) and the confirmatory window (section 4).

Endpoint properties were measured on 17 Oct 2026 (probes/ and RUNLOG). Changing a route is operational, not analytic,
but every change is logged in RUNLOG because the hashes of this file are part of the CODE FREEZE.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional

from .common import POOLS, PoolSpec, MINUTE
from .transport import BlockscoutTransport, GatedTransport, RateLimiter

# ----------------------------------------------------------------------------- confirmatory window W (section 4)
W_START_TS = 1_704_067_200          # 2024-01-01 00:00:00 UTC
W_END_TS_EXCL = 1_790_812_800       # 2026-10-01 00:00:00 UTC (exclusive)
W_START_MIN = W_START_TS // MINUTE
W_END_MIN_EXCL = W_END_TS_EXCL // MINUTE
SERIES_MARGIN_BEFORE_MIN = 24 * 60              # clock series starts one day before W (lag + forward fill, N13/N19)
SERIES_MARGIN_AFTER_MIN = 7 * 24 * 60           # and runs one week past W (chaining, truncation flags, N19)
SERIES_START_MIN = W_START_MIN - SERIES_MARGIN_BEFORE_MIN
SERIES_END_MIN_EXCL = W_END_MIN_EXCL + SERIES_MARGIN_AFTER_MIN
SEED = 20261017

DRPC_DEFAULT_RATE = 2.0             # req/s used only if no measured rate file exists (section 8.2: throttled)
DRPC_SAFETY = 0.8                   # pull rate = measured sustained safe rate x this factor
DRPC_RATE_FILE = Path(__file__).resolve().parent.parent / "probes" / "drpc_rate.json"


@dataclass(frozen=True)
class ChainRoute:
    chain: str
    logs_url: str
    log_cap: int                    # largest block span one eth_getLogs may cover on this endpoint
    logs_pause_s: float
    calls_url: str                  # archive eth_call (band reads, slot0)
    call_batch: int                 # JSON-RPC batch size accepted for eth_call
    headers_url: str                # eth_getBlockByNumber (bisection, header timestamps, cross-checks)
    header_batch: int
    timestamps: str                 # "log_field" (Ethereum, D9) | "headers" (6.6) | "formula" (6.6 OP-stack)
    secondary_logs_url: Optional[str]   # completeness second source (8.4); None = none exists
    fallback_logs_api: Optional[str]    # Blockscout REST for Base (8.2 fallback order)
    paced: bool = False             # True = pace every request with the measured drpc rate


ROUTES: Dict[str, ChainRoute] = {
    "ethereum": ChainRoute("ethereum", "https://rpc.mevblocker.io", 10_000, 0.3, "https://rpc.mevblocker.io", 100,
                           "https://rpc.mevblocker.io", 100, "log_field", "https://eth.drpc.org", None),
    "arbitrum": ChainRoute("arbitrum", "https://arb1.arbitrum.io/rpc", 50_000, 0.3, "https://arb1.arbitrum.io/rpc", 50,
                           "https://arb1.arbitrum.io/rpc", 50, "headers", "https://arbitrum-one-rpc.publicnode.com", None),
    "base": ChainRoute("base", "https://base.drpc.org", 100, 0.0, "https://base.drpc.org", 3,
                       "https://base.drpc.org", 3, "formula", "https://base-rpc.publicnode.com",
                       "https://base.blockscout.com/api", paced=True),
    "unichain": ChainRoute("unichain", "https://mainnet.unichain.org", 10_000, 0.3, "https://mainnet.unichain.org", 10,
                           "https://mainnet.unichain.org", 10, "formula", "https://unichain-rpc.publicnode.com", None),
}


ROUTES_OVERRIDE_FILE = Path(__file__).resolve().parent.parent / "data" / "confirmatory" / "routes_override.json"


def load_routes(override_path: Optional[Path] = None) -> Dict[str, ChainRoute]:
    """ROUTES with operational overrides from a JSON file {chain: {field: value}} (endpoint URLs, caps, batch sizes,
    timestamp mode). Endpoints are operational parameters, not analysis rules: a route change is recorded in RUNLOG with
    the override file's content and leaves the frozen code untouched."""
    path = ROUTES_OVERRIDE_FILE if override_path is None else override_path
    routes = dict(ROUTES)
    if path.is_file():
        over = json.loads(path.read_text(encoding="utf-8"))
        for chain, fields in over.items():
            if chain not in routes:
                raise ValueError(f"routes override names an unknown chain {chain!r}")
            unknown = set(fields) - set(ChainRoute.__dataclass_fields__)
            if unknown:
                raise ValueError(f"routes override for {chain}: unknown fields {sorted(unknown)}")
            routes[chain] = replace(routes[chain], **fields)
    return routes


def pools_on(chain: str) -> list:
    return [p for p in POOLS.values() if p.chain == chain]


def drpc_rate_per_s(path: Path = DRPC_RATE_FILE) -> float:
    """Pull pacing for drpc: DRPC_SAFETY x the measured sustained safe rate (probes/drpc_rate.json), else the default."""
    if path.is_file():
        d = json.loads(path.read_text(encoding="utf-8"))
        return float(d["safe_rate_per_s"]) * DRPC_SAFETY
    return DRPC_DEFAULT_RATE


@dataclass
class Live:
    """The transports one chain's drivers use. Tests substitute FakeTransport objects field by field."""
    route: ChainRoute
    logs: object
    calls: object
    headers: object
    secondary: Optional[object] = None
    fallback: Optional[object] = None


def build_live(route: ChainRoute, drpc_rate: Optional[float] = None) -> Live:
    limiter = RateLimiter(drpc_rate or drpc_rate_per_s()) if route.paced else None
    logs = GatedTransport(route.logs_url, timeout_s=90, pause_s=route.logs_pause_s, limiter=limiter)
    calls = logs if route.calls_url == route.logs_url else GatedTransport(route.calls_url, timeout_s=60, pause_s=0.3, limiter=limiter)
    headers = logs if route.headers_url == route.logs_url else GatedTransport(route.headers_url, timeout_s=60, pause_s=0.2, limiter=limiter)
    secondary = GatedTransport(route.secondary_logs_url, timeout_s=60, pause_s=0.3) if route.secondary_logs_url else None
    fallback = BlockscoutTransport(route.fallback_logs_api, pause_s=0.5) if route.fallback_logs_api else None
    return Live(route, logs, calls, headers, secondary, fallback)


def with_timestamps(route: ChainRoute, mode: str) -> ChainRoute:
    """Operational override: e.g. Ethereum back to headers after a blockTimestamp mismatch (D9), or an OP-stack chain to
    headers after a failed formula verification (6.6)."""
    if mode not in ("log_field", "headers", "formula"):
        raise ValueError(mode)
    return replace(route, timestamps=mode)
