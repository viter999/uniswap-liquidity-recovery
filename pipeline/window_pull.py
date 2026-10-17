"""One pool-episode window pull with provenance (prereg v0.3 sections 5.3, 6.5, 6.6, 8.4; deviations D9, D13).

Outputs under <out>/<pool>/ep<id>/: logs.csv (Mint/Burn/Swap events in (block, log_index) order), timestamps.npz
(log-bearing blocks and their timestamps) and meta.json (counts, sources, timestamp checks). Finished windows are
reused on a re-run. Logs are streamed range by range so the own-memory cap holds on the busiest Base windows.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import numpy as np

from .blocks import HeaderCache, cross_check_timestamps, timestamps_from_logs
from .chains import Live
from .common import BURN_TOPIC, MINT_TOPIC, SWAP_TOPIC, PoolSpec, timestamp_from_block
from .pull_logs import KNOWN_TOPICS, decode_log, dedupe, fetch_headers, header_gaps, iter_ranges
from .reconstruct import Event
from .transport import Stats, get_logs_with_fallback, log, retry

CSV_COLS = ["block", "log_index", "kind", "tick_lower", "tick_upper", "amount", "sqrt_price_x96", "liquidity", "tick"]
ALL_TOPICS = [[MINT_TOPIC, BURN_TOPIC, SWAP_TOPIC]]


def pool_dir_name(pool: PoolSpec) -> str:
    return pool.key.replace(":", "_").replace("%", "")


def window_dir(out: Path, pool: PoolSpec, episode_id: int) -> Path:
    return Path(out) / pool_dir_name(pool) / f"ep{episode_id}"


@dataclass
class WindowMeta:
    pool_key: str
    episode_id: int
    b0: int
    b_end: int
    timestamps: str = ""
    logs_raw: int = 0
    logs_deduped: int = 0
    events: int = 0
    unknown_topic: int = 0
    log_bearing_blocks: int = 0
    sources: Dict[str, int] = field(default_factory=dict)      # ranges served by "primary" / "fallback"
    ts_check: dict = field(default_factory=dict)              # Ethereum log-field cross-check (D9)
    header_gaps: list = field(default_factory=list)           # Ethereum/Arbitrum (6.5)
    complete: bool = False


def event_row(ev: Event) -> list:
    return [ev.block, ev.log_index, ev.kind, ev.tick_lower, ev.tick_upper, ev.amount, ev.sqrt_price_x96, ev.liquidity, ev.tick]


def row_event(row: dict) -> Event:
    return Event(int(row["block"]), int(row["log_index"]), row["kind"], int(row["tick_lower"]), int(row["tick_upper"]),
                 int(row["amount"]), int(row["sqrt_price_x96"]), int(row["liquidity"]), int(row["tick"]))


def read_events(path: Path) -> Iterator[Event]:
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            yield row_event(row)


def _timestamps(live: Live, blocks: np.ndarray, ts_map: Dict[int, int], meta: WindowMeta,
                header_cache: Optional[HeaderCache]) -> np.ndarray:
    route = live.route
    if route.timestamps == "log_field":
        ts = np.array([ts_map[int(n)] for n in blocks], dtype=np.int64)
        meta.ts_check = cross_check_timestamps(live.headers, {int(n): int(t) for n, t in zip(blocks, ts)}, route.header_batch,
                                               cache=header_cache)
        if not meta.ts_check["ok"]:
            raise SystemExit(f"TIMESTAMP MISMATCH {meta.pool_key} ep{meta.episode_id}: {meta.ts_check['mismatches'][:5]} "
                             f"-- stop; rerun with --eth-timestamps headers (6.6 route) and report (ruling 17 Oct 2026)")
        return ts
    if route.timestamps == "headers":
        hdr: Dict[int, dict] = {}
        for i in range(0, len(blocks), route.header_batch):
            chunk = [int(n) for n in blocks[i:i + route.header_batch]]
            hdr.update(retry(lambda c=chunk: fetch_headers(live.headers, c, route.header_batch),
                             f"headers {meta.pool_key} ep{meta.episode_id} {chunk[0]}"))
            Stats.gate()
        ts = np.array([hdr[int(n)]["timestamp"] for n in blocks], dtype=np.int64)
        if header_cache is not None:
            header_cache.add_many((n, h["timestamp"]) for n, h in hdr.items())
        meta.header_gaps = header_gaps({int(n): int(t) for n, t in zip(blocks, ts)})
        return ts
    return np.array([timestamp_from_block(route.chain, int(n)) for n in blocks], dtype=np.int64)


def pull_window(live: Live, pool: PoolSpec, episode_id: int, b0: int, b_end: int, out: Path,
                header_cache: Optional[HeaderCache] = None) -> WindowMeta:
    """Mint/Burn/Swap logs of `pool` over (b0, b_end], their timestamps and the provenance record."""
    d = window_dir(out, pool, episode_id)
    meta_f = d / "meta.json"
    if meta_f.exists():
        m = WindowMeta(**json.loads(meta_f.read_text(encoding="utf-8")))
        if m.complete and (m.b0, m.b_end) == (b0, b_end):
            return m
    d.mkdir(parents=True, exist_ok=True)
    route = live.route
    meta = WindowMeta(pool.key, episode_id, b0, b_end, timestamps=route.timestamps)
    blocks: set = set()
    ts_map: Dict[int, int] = {}
    tmp = d / "logs.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLS)
        for a, b in iter_ranges(b0 + 1, b_end, route.log_cap):
            raw, src = get_logs_with_fallback(live.logs, live.fallback, pool.address, ALL_TOPICS, a, b,
                                              f"logs {pool.key} ep{episode_id} {a}-{b}")
            meta.sources[src] = meta.sources.get(src, 0) + 1
            meta.logs_raw += len(raw)
            ded = dedupe(raw)
            meta.logs_deduped += len(ded)
            if route.timestamps == "log_field":
                ts_map.update(timestamps_from_logs(ded))
            for lg in ded:
                if lg["topics"][0] not in KNOWN_TOPICS:
                    meta.unknown_topic += 1
                    continue
                ev = decode_log(lg)
                blocks.add(ev.block)
                meta.events += 1
                w.writerow(event_row(ev))
            Stats.gate()
    tmp.replace(d / "logs.csv")
    bl = np.array(sorted(blocks), dtype=np.int64)
    meta.log_bearing_blocks = len(bl)
    ts = _timestamps(live, bl, ts_map, meta, header_cache) if len(bl) else np.zeros(0, dtype=np.int64)
    if np.any(np.diff(ts) < 0):
        raise SystemExit(f"ERROR non-monotone timestamps in {pool.key} ep{episode_id}")
    np.savez(d / "timestamps.npz", blocks=bl, timestamps=ts)
    meta.complete = True
    meta_f.write_text(json.dumps(asdict(meta), indent=1), encoding="utf-8")
    log(f"window {pool.key} ep{episode_id}: {meta.events} events over {b_end - b0} blocks, {meta.log_bearing_blocks} log-bearing, "
        f"sources {meta.sources}, ts {route.timestamps}")
    return meta


def load_window_clock(d: Path):
    z = np.load(d / "timestamps.npz")
    return z["blocks"], z["timestamps"]
