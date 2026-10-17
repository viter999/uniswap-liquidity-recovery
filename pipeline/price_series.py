"""Price-only phase (prereg v0.3 section 5.6): minute series from the clock pool's swaps, slot0 reads for the other pools,
local confirmation, N_conf and the primary-threshold decision. Reads nothing but prices.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .common import PoolSpec, Q96, price_usd_per_eth, minute_of
from .episodes import (ALTERNATE, PRIMARY, Episode, Params, ThresholdDecision, confirmatory, decide_primary,
                       detect_episodes, forward_fill, local_confirmed, n_conf)
from .reconstruct import Event

SLOT0_SELECTOR = "0x3850c7bd"
PriceAt = Callable[[str, int], float]   # (pool_key, minute) -> p ; production: slot0 at B(m); tests: dict lookup


def minute_observations(swaps: Iterable[Event], timestamps: Dict[int, int], pool: PoolSpec) -> Dict[int, float]:
    """minute -> p of the last swap in that minute, ordered by (block, log_index) (5.1)."""
    best: Dict[int, Tuple[Tuple[int, int], float]] = {}
    for ev in swaps:
        if ev.kind != "swap":
            continue
        m = minute_of(timestamps[ev.block])
        key = ev.order
        if m not in best or key > best[m][0]:
            best[m] = (key, price_usd_per_eth(ev.sqrt_price_x96, pool))
    return {m: v for m, (_, v) in best.items()}


def decode_slot0(result_hex: str, pool: PoolSpec) -> float:
    """eth_call slot0 -> p (first 32-byte word is sqrtPriceX96)."""
    h = result_hex[2:] if result_hex.startswith("0x") else result_hex
    return price_usd_per_eth(int(h[:64], 16), pool)


def local_flags(episodes: Sequence[Episode], non_clock_pools: Sequence[PoolSpec], price_at: PriceAt,
                params: Params) -> Dict[int, Dict[str, bool]]:
    """{episode_id: {pool_key: local_shock}} using p(t0) and p(t1_first) per pool (5.5)."""
    out: Dict[int, Dict[str, bool]] = {}
    for e in episodes:
        flags = {}
        for pool in non_clock_pools:
            p0, p1 = price_at(pool.key, e.t0), price_at(pool.key, e.t1_first)
            flags[pool.key] = local_confirmed(p0, p1, e.first_dlog, params) if (p0 and p1) else False
        out[e.episode_id] = flags
    return out


def run_price_only_phase(clock_obs: Dict[int, float], series_start: int, series_end_exclusive: int,
                         w_start: int, w_end_exclusive: int, non_clock_pools: Sequence[PoolSpec],
                         price_at: PriceAt, nconf_pool_keys: Optional[Sequence[str]] = None,
                         episodes_by_set: Optional[Dict[str, List[Episode]]] = None) -> dict:
    """Episodes for both parameter sets, local flags, N_conf and the decision (5.6 steps 2-5).

    N_conf counts the confirmatory non-clock pools only (5.6 step 4); an exploratory pool in non_clock_pools gets its
    local flags computed but does not enter N_conf. episodes_by_set lets a driver pass episodes it detected earlier."""
    prices = forward_fill(clock_obs, series_start, series_end_exclusive) if episodes_by_set is None else None
    keys = list(nconf_pool_keys) if nconf_pool_keys is not None else [p.key for p in non_clock_pools if p.role == "confirmatory"]
    out = {"series_start": series_start, "sets": {}}
    counts = {}
    for params in (PRIMARY, ALTERNATE):
        if episodes_by_set is not None:
            eps = episodes_by_set[params.name]
        else:
            eps = confirmatory(detect_episodes(series_start, prices, params), w_start, w_end_exclusive)
        flags = local_flags(eps, non_clock_pools, price_at, params)
        nc = n_conf(flags, keys)
        counts[params.name] = (len(eps), nc)
        out["sets"][params.name] = {"episodes": [asdict(e) for e in eps], "local_flags": flags, "n": len(eps), "n_conf": nc}
    n_p, nc_p = counts[PRIMARY.name]
    n_a, nc_a = counts[ALTERNATE.name]
    dec = decide_primary(n_p, nc_p, n_a, nc_a)
    out["decision"] = asdict(dec)
    return out


def write_decision(result: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
