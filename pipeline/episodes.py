"""Episode detection, consecutive-episode flags, local confirmation and the threshold decision (prereg v0.3 sections 5.2-5.6).

All times are minute indices (minutes since the Unix epoch, UTC). Prices are p = USD per ETH (section 5.1).
Log moves are natural-log units.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

HORIZON_MIN = 72 * 60           # 4,320 min censoring horizon (5.3)
PERSIST_MIN = 15                # persistence window for recovery (6.3)
PRE_START_MIN = 180             # pre-shock window [t0-180, t0-15] (5.3)
PRE_END_MIN = 15
DIP_AFTER_MIN = 30              # dip window [t0, t1_last+30] (5.3)
WINDOW_BEFORE_MIN = 4 * 60      # data window starts t0-4h (5.3)
GAP_MIN = 48 * 60               # chaining gap (5.2)
LONG_CLUSTER_MIN = 48 * 60      # span > 48 h -> long_cluster (5.2)
LOCAL_FACTOR = 0.8              # local confirmation at 0.8*theta (5.5)
NCONF_FLOOR = 24                # switch rule (5.6 step 5)


@dataclass(frozen=True)
class Params:
    theta: float
    lag_min: int
    name: str

    @property
    def local_theta(self) -> float:
        return LOCAL_FACTOR * self.theta


PRIMARY = Params(0.05, 60, "5pct_60min")
ALTERNATE = Params(0.03, 30, "3pct_30min")


@dataclass
class Episode:
    episode_id: int
    t1_first: int
    t1_last: int
    t0: int
    n_trigger_minutes: int
    max_abs_dlog: float
    first_dlog: float
    direction: str                       # "ETH_up" | "ETH_down"
    long_cluster: bool
    truncated_by_next: bool = False
    censor_min: int = HORIZON_MIN        # min(4320, t0(next) - t1_last) when truncated (5.4)
    pre_in_prior_recovery: bool = False
    params: str = ""

    @property
    def span_min(self) -> int:
        return self.t1_last - self.t1_first

    @property
    def window_start(self) -> int:
        return self.t0 - WINDOW_BEFORE_MIN

    @property
    def window_end(self) -> int:
        """Last sampled minute: t1_last + 72 h + 15 min (5.3)."""
        return self.t1_last + HORIZON_MIN + PERSIST_MIN

    @property
    def pre_window(self):
        return self.t0 - PRE_START_MIN, self.t0 - PRE_END_MIN

    @property
    def dip_window(self):
        return self.t0, self.t1_last + DIP_AFTER_MIN


# ----------------------------------------------------------------------------- series building
def forward_fill(obs: Dict[int, float], start_minute: int, end_minute_exclusive: int) -> np.ndarray:
    """Dense minute grid [start, end) with the last observed price carried forward; NaN before the first observation.

    obs maps minute index -> price of the last swap in that minute (5.1)."""
    n = end_minute_exclusive - start_minute
    out = np.full(n, np.nan)
    last = np.nan
    for k in range(n):
        v = obs.get(start_minute + k)
        if v is not None:
            last = v
        out[k] = last
    return out


# ----------------------------------------------------------------------------- detection (5.2)
def trigger_minutes(start_minute: int, prices: np.ndarray, params: Params) -> List[tuple]:
    """[(minute, dlog)] where |log(p(m)/p(m-L))| >= theta; NaNs are skipped."""
    logs = np.log(prices)
    lag = params.lag_min
    d = logs[lag:] - logs[:-lag]
    idx = np.flatnonzero(np.abs(d) >= params.theta)
    return [(start_minute + lag + int(k), float(d[k])) for k in idx if not math.isnan(d[k])]


def chain_triggers(trigs: Sequence[tuple], params: Params) -> List[Episode]:
    """Gap-based chaining: a trigger starts a new episode iff >= GAP_MIN after the previous trigger minute."""
    episodes: List[Episode] = []
    cur: Optional[dict] = None
    for m, dlog in trigs:
        if cur is None or m - cur["last"] >= GAP_MIN:
            if cur is not None:
                episodes.append(_finish(cur, len(episodes) + 1, params))
            cur = {"first": m, "last": m, "n": 0, "max": 0.0, "first_dlog": dlog}
        cur["last"] = m
        cur["n"] += 1
        cur["max"] = max(cur["max"], abs(dlog))
    if cur is not None:
        episodes.append(_finish(cur, len(episodes) + 1, params))
    return episodes


def _finish(cur: dict, eid: int, params: Params) -> Episode:
    t1_first, t1_last = cur["first"], cur["last"]
    return Episode(
        episode_id=eid, t1_first=t1_first, t1_last=t1_last, t0=t1_first - params.lag_min,
        n_trigger_minutes=cur["n"], max_abs_dlog=cur["max"], first_dlog=cur["first_dlog"],
        direction="ETH_up" if cur["first_dlog"] > 0 else "ETH_down",
        long_cluster=(t1_last - t1_first) > LONG_CLUSTER_MIN, params=params.name,
    )


def direction_over(prices: np.ndarray, start_minute: int, t0: int, t1_first: int) -> float:
    """log(p(t1_first) / p(t0)) on a dense series (5.2 direction; 5.5 local move)."""
    return float(math.log(prices[t1_first - start_minute]) - math.log(prices[t0 - start_minute]))


def flag_consecutive(episodes: List[Episode]) -> List[Episode]:
    """Section 5.4: truncated_by_next on e, pre_in_prior_recovery on e+1."""
    eps = sorted(episodes, key=lambda e: e.t1_first)
    for a, b in zip(eps, eps[1:]):
        if b.t0 < a.t1_last + HORIZON_MIN:
            a.truncated_by_next = True
            a.censor_min = min(HORIZON_MIN, b.t0 - a.t1_last)
        if b.t0 - PRE_START_MIN < a.t1_last + HORIZON_MIN:
            b.pre_in_prior_recovery = True
    return eps


def detect_episodes(start_minute: int, prices: np.ndarray, params: Params) -> List[Episode]:
    """Full section 5.2 + 5.4 on the clock series."""
    trigs = trigger_minutes(start_minute, prices, params)
    eps = chain_triggers(trigs, params)
    # direction is defined over [t0, t1_first] on the clock series (5.2), which equals first_dlog by construction
    return flag_consecutive(eps)


def confirmatory(episodes: Iterable[Episode], w_start_minute: int, w_end_minute_exclusive: int) -> List[Episode]:
    """Episode is confirmatory iff t1_first in W (section 4)."""
    return [e for e in episodes if w_start_minute <= e.t1_first < w_end_minute_exclusive]


# ----------------------------------------------------------------------------- local confirmation (5.5)
def local_confirmed(p_t0: float, p_t1: float, clock_dlog: float, params: Params) -> bool:
    """|log(p(t1_first)/p(t0))| >= 0.8*theta with the same sign as the clock pool's move."""
    d = math.log(p_t1) - math.log(p_t0)
    return abs(d) >= params.local_theta and (d > 0) == (clock_dlog > 0) and d != 0.0


# ----------------------------------------------------------------------------- threshold decision (5.6)
@dataclass
class ThresholdDecision:
    n_primary: int
    nconf_primary: int
    n_alternate: int
    nconf_alternate: int
    chosen: str
    floor: int = NCONF_FLOOR
    detail: dict = field(default_factory=dict)


def n_conf(local_flags: Dict[int, Dict[str, bool]], non_clock_pool_keys: Sequence[str]) -> int:
    """Episodes with local_shock = true in every non-clock confirmatory pool (5.6 step 4)."""
    return sum(1 for flags in local_flags.values() if all(flags.get(k, False) for k in non_clock_pool_keys))


def decide_primary(n_p: int, nconf_p: int, n_a: int, nconf_a: int) -> ThresholdDecision:
    """If N_conf(primary) < 24 the primary set becomes the alternate (5.6 step 5)."""
    chosen = ALTERNATE.name if nconf_p < NCONF_FLOOR else PRIMARY.name
    return ThresholdDecision(n_p, nconf_p, n_a, nconf_a, chosen)
