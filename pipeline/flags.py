"""Episode flags from the frozen lists in prereg v0.3 section 10.2 (incentives, structural events, S4 outages).

Windows are UTC minute indices derived from the dates fixed in the prereg. Changing any of them after the freeze is a
logged deviation, not a code edit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Tuple

from .episodes import Episode, HORIZON_MIN, PRE_START_MIN


def _m(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()) // 60


# incentive / structural flags: (flag, chains or None for all, start minute inclusive, end minute inclusive)
INCENTIVE_WINDOWS: List[Tuple[str, Tuple[str, ...], int, int]] = [
    ("incentive_possible", ("arbitrum",), _m("2000-01-01T00:00"), _m("2024-07-24T00:00") - 1),      # I1: t0 < 2024-07-24
    ("incentive_possible", ("arbitrum",), _m("2024-06-17T00:00"), _m("2024-09-09T23:59")),          # I2
    ("incentive_v4_pull", ("ethereum", "arbitrum", "base"), _m("2025-03-19T00:00"), _m("2025-09-30T23:59")),  # I3
    ("incentive_unichain", ("unichain",), _m("2025-04-14T00:00"), _m("2026-04-14T23:59")),          # I4
    ("structural_fee_switch", ("ethereum", "arbitrum", "base", "unichain"), _m("2025-12-28T00:00"), _m("2100-01-01T00:00")),  # S2
    ("structural_fee_switch_all", ("ethereum", "arbitrum", "base", "unichain"), _m("2026-07-27T00:00"), _m("2100-01-01T00:00")),  # S3
]

# S4 outage list (outages/outage_list_20261017.md): (chain, start minute, end minute inclusive, kind)
OUTAGES: List[Tuple[str, int, int, str]] = [
    ("base", _m("2025-08-05T06:07"), _m("2025-08-05T06:40"), "halt"),        # O1
    ("base", _m("2026-06-25T15:47"), _m("2026-06-25T17:58"), "halt"),        # O2
    ("base", _m("2026-06-26T15:28"), _m("2026-06-26T15:48"), "halt"),        # O3
    ("base", _m("2026-05-13T16:44"), _m("2026-05-14T05:04"), "degraded"),    # O4
    ("base", _m("2026-02-05T15:22"), _m("2026-02-06T01:46"), "degraded"),    # O5
    ("unichain", _m("2025-10-03T18:33"), _m("2025-10-03T19:42"), "halt"),    # O6 (timezone treated as UTC, [VERIFY])
]


def incentive_flags(ep: Episode, chain: str) -> Dict[str, bool]:
    out = {f: False for f, *_ in INCENTIVE_WINDOWS}
    for flag, chains, lo, hi in INCENTIVE_WINDOWS:
        if chain in chains and lo <= ep.t0 <= hi:
            out[flag] = True
    return out


def outage_flags(ep: Episode, chain: str) -> Dict[str, bool]:
    """outage: window [t0-180, t1_last+72h] overlaps a halt; chain_degraded: overlaps a degraded entry (6.5)."""
    lo, hi = ep.t0 - PRE_START_MIN, ep.t1_last + HORIZON_MIN
    out = {"outage": False, "chain_degraded": False}
    for c, a, b, kind in OUTAGES:
        if c == chain and a <= hi and b >= lo:
            out["outage" if kind == "halt" else "chain_degraded"] = True
    return out
