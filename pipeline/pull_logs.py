"""Log and header pulling over JSON-RPC (prereg v0.3 sections 6.6, 8.2, 8.4).

Nothing here contacts a network unless a live Transport is passed in; tests use FakeTransport.
The CurlTransport is included because Python 3.9's urllib was rejected by the public endpoints during probing.
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .common import BURN_TOPIC, MINT_TOPIC, SWAP_TOPIC, CHAIN_TIME, parse_int24, parse_int256, timestamp_from_block
from .reconstruct import Event

HEADER_BATCH = 100
COMPLETENESS_SAMPLE = 50
SEED = 20261017
KNOWN_TOPICS = frozenset((SWAP_TOPIC, MINT_TOPIC, BURN_TOPIC))   # other pool logs (Collect, Flash, ...) are skipped


class RpcError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- transports
class Transport:
    """Minimal JSON-RPC interface: call(method, params) and batch([(method, params), ...])."""

    def call(self, method: str, params: list) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def batch(self, calls: Sequence[Tuple[str, list]]) -> List[Any]:
        return [self.call(m, p) for m, p in calls]


class CurlTransport(Transport):
    """curl-based transport (the probes showed urllib is blocked by UA filtering / stale SSL chains)."""

    def __init__(self, url: str, timeout_s: int = 30, pause_s: float = 0.1):
        self.url, self.timeout_s, self.pause_s = url, timeout_s, pause_s
        self.calls = 0

    def _post(self, body: str) -> Any:
        proc = subprocess.run(["curl", "-sS", "-m", str(self.timeout_s), "-X", "POST", self.url,
                               "-H", "content-type: application/json", "--data", body],
                              capture_output=True, text=True, timeout=self.timeout_s + 5, check=False)
        self.calls += 1
        time.sleep(self.pause_s)
        if proc.returncode != 0:
            raise RpcError(f"curl rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return json.loads(proc.stdout)

    def call(self, method: str, params: list) -> Any:
        out = self._post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
        if "error" in out:
            raise RpcError(str(out["error"])[:300])
        return out["result"]

    def batch(self, calls: Sequence[Tuple[str, list]]) -> List[Any]:
        body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)])
        out = self._post(body)
        if isinstance(out, dict):  # endpoint does not support batching
            raise RpcError(f"batch rejected: {str(out)[:200]}")
        by_id = {o["id"]: o for o in out}
        res = []
        for i in range(len(calls)):
            o = by_id.get(i)
            if o is None or "error" in (o or {}):
                raise RpcError(f"batch item {i} failed: {str(o)[:200]}")
            res.append(o["result"])
        return res


@dataclass
class FakeChain:
    """Synthetic chain for tests: blocks {n: (timestamp, hash)} and logs."""
    blocks: Dict[int, Tuple[int, str]]
    logs: List[dict]
    log_range_cap: int = 100
    serves_logs: bool = True
    result_cap: Optional[int] = None      # mimic an endpoint result cap ("query returned more than N results")
    latest_override: Optional[int] = None

    @property
    def latest(self) -> int:
        return self.latest_override if self.latest_override is not None else max(self.blocks)


class FakeTransport(Transport):
    """archive(to, data, block) answers eth_call at a block tag; code_from_block makes eth_getCode non-empty from there."""

    def __init__(self, chain: FakeChain, archive=None, code_from_block: Optional[int] = None):
        self.chain = chain
        self.archive = archive
        self.code_from_block = code_from_block
        self.calls = 0
        self.batches = 0

    def call(self, method: str, params: list) -> Any:
        self.calls += 1
        c = self.chain
        if method == "eth_blockNumber":
            return hex(c.latest)
        if method == "eth_call":
            if self.archive is None:
                raise RpcError("fake chain has no archive")
            return self.archive(params[0]["to"], params[0]["data"], int(params[1], 16))
        if method == "eth_getCode":
            n = int(params[1], 16)
            return "0x6080" if (self.code_from_block is not None and n >= self.code_from_block) else "0x"
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            if n not in c.blocks:
                return None
            ts, h = c.blocks[n]
            return {"number": hex(n), "timestamp": hex(ts), "hash": h, "transactions": []}
        if method == "eth_getLogs":
            if not c.serves_logs:
                raise RpcError("request limit reached")
            f = params[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if b - a + 1 > c.log_range_cap:
                raise RpcError(f"block range greater than {c.log_range_cap} max")
            addr = f.get("address", "").lower()
            topic0 = (f.get("topics") or [None])[0]
            out = []
            for lg in c.logs:
                n = int(lg["blockNumber"], 16)
                t0 = lg["topics"][0]
                hit = topic0 is None or (t0 in topic0 if isinstance(topic0, list) else t0 == topic0)
                if a <= n <= b and lg["address"].lower() == addr and hit:
                    out.append(dict(lg))
            if c.result_cap is not None and len(out) > c.result_cap:
                raise RpcError(f"query returned more than {c.result_cap} results")
            return out
        raise RpcError(f"unsupported method {method}")

    def batch(self, calls):
        self.batches += 1
        return [self.call(m, p) for m, p in calls]


# ----------------------------------------------------------------------------- ranges, logs, dedupe
def iter_ranges(from_block: int, to_block: int, cap: int) -> List[Tuple[int, int]]:
    """Inclusive block ranges of at most `cap` blocks covering [from_block, to_block]."""
    if cap < 1 or to_block < from_block:
        return []
    out, a = [], from_block
    while a <= to_block:
        b = min(a + cap - 1, to_block)
        out.append((a, b))
        a = b + 1
    return out


def get_logs(transport: Transport, address: str, topics: Optional[List[str]], from_block: int, to_block: int,
             cap: int) -> List[dict]:
    raw: List[dict] = []
    for a, b in iter_ranges(from_block, to_block, cap):
        flt = {"address": address, "fromBlock": hex(a), "toBlock": hex(b)}
        if topics:
            flt["topics"] = topics
        raw.extend(transport.call("eth_getLogs", [flt]))
    return raw


def dedupe(logs: Iterable[dict]) -> List[dict]:
    """Drop removed:true logs and duplicates on (blockHash, logIndex); keep first occurrence (8.4)."""
    seen, out = set(), []
    for lg in logs:
        if lg.get("removed"):
            continue
        key = (lg["blockHash"].lower(), int(lg["logIndex"], 16))
        if key in seen:
            continue
        seen.add(key)
        out.append(lg)
    out.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
    return out


# ----------------------------------------------------------------------------- headers and timestamps (6.6)
def fetch_headers(transport: Transport, block_numbers: Iterable[int], batch_size: int = HEADER_BATCH) -> Dict[int, dict]:
    """{n: {"timestamp": int, "hash": str}} via batched eth_getBlockByNumber(n, false)."""
    nums = sorted(set(block_numbers))
    out: Dict[int, dict] = {}
    for i in range(0, len(nums), batch_size):
        chunk = nums[i:i + batch_size]
        res = transport.batch([("eth_getBlockByNumber", [hex(n), False]) for n in chunk])
        for n, blk in zip(chunk, res):
            if blk is None:
                raise RpcError(f"header {n} missing")
            out[n] = {"timestamp": int(blk["timestamp"], 16), "hash": blk["hash"]}
    return out


def timestamps_for(chain: str, block_numbers: Iterable[int], transport: Optional[Transport] = None) -> Dict[int, int]:
    """Formula on OP-stack chains; headers elsewhere (6.6)."""
    nums = sorted(set(block_numbers))
    if chain in CHAIN_TIME:
        return {n: timestamp_from_block(chain, n) for n in nums}
    if transport is None:
        raise RpcError(f"{chain} needs a transport for headers")
    return {n: h["timestamp"] for n, h in fetch_headers(transport, nums).items()}


def verify_formula(transport: Transport, chain: str, lo_block: int, hi_block: int, n: int = COMPLETENESS_SAMPLE,
                   seed: int = SEED) -> dict:
    """50 random blocks in [lo, hi]: header timestamp must equal the formula exactly (6.6)."""
    rng = random.Random(seed)
    sample = sorted(rng.sample(range(lo_block, hi_block + 1), min(n, hi_block - lo_block + 1)))
    headers = fetch_headers(transport, sample)
    mism = [b for b in sample if headers[b]["timestamp"] != timestamp_from_block(chain, b)]
    return {"chain": chain, "sampled": len(sample), "mismatches": mism, "ok": not mism}


def header_gaps(timestamps: Dict[int, int], max_gap_s: int = 600) -> List[Tuple[int, int, int]]:
    """(n, n+1, gap) for fetched blocks adjacent by number whose timestamps differ by more than max_gap_s (6.5)."""
    out = []
    for n in sorted(timestamps):
        if n + 1 in timestamps and timestamps[n + 1] - timestamps[n] > max_gap_s:
            out.append((n, n + 1, timestamps[n + 1] - timestamps[n]))
    return out


def cutoff(transport: Transport) -> dict:
    """Latest block number and hash at pull time (8.4)."""
    n = int(transport.call("eth_blockNumber", []), 16)
    blk = transport.call("eth_getBlockByNumber", [hex(n), False])
    return {"block": n, "hash": blk["hash"]}


# ----------------------------------------------------------------------------- decoding
def _words(data_hex: str) -> List[str]:
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def decode_log(lg: dict) -> Event:
    """Uniswap v3 Mint / Burn / Swap log -> Event. Raises RpcError on an unknown topic."""
    t0 = lg["topics"][0]
    blk, idx = int(lg["blockNumber"], 16), int(lg["logIndex"], 16)
    w = _words(lg["data"])
    if t0 == SWAP_TOPIC:
        return Event(blk, idx, "swap", sqrt_price_x96=int(w[2], 16), liquidity=int(w[3], 16), tick=parse_int24(w[4]))
    if t0 == MINT_TOPIC:
        return Event(blk, idx, "mint", tick_lower=parse_int24(lg["topics"][2][2:]), tick_upper=parse_int24(lg["topics"][3][2:]),
                     amount=int(w[1], 16))
    if t0 == BURN_TOPIC:
        return Event(blk, idx, "burn", tick_lower=parse_int24(lg["topics"][2][2:]), tick_upper=parse_int24(lg["topics"][3][2:]),
                     amount=int(w[0], 16))
    raise RpcError(f"unknown topic {t0}")


def encode_event(ev: Event, address: str, block_hash: str, block_timestamp: Optional[int] = None) -> dict:
    """Inverse of decode_log for synthetic tests; block_timestamp adds the endpoint's `blockTimestamp` field (D9)."""
    def word(v: int) -> str:
        return format(v % (1 << 256), "064x")

    if ev.kind == "swap":
        data = "0x" + word(0) + word(0) + word(ev.sqrt_price_x96) + word(ev.liquidity) + word(ev.tick)
        topics = [SWAP_TOPIC, "0x" + word(0), "0x" + word(0)]
    elif ev.kind == "mint":
        data = "0x" + word(0) + word(ev.amount) + word(0) + word(0)
        topics = [MINT_TOPIC, "0x" + word(0), "0x" + word(ev.tick_lower), "0x" + word(ev.tick_upper)]
    elif ev.kind == "burn":
        data = "0x" + word(ev.amount) + word(0) + word(0)
        topics = [BURN_TOPIC, "0x" + word(0), "0x" + word(ev.tick_lower), "0x" + word(ev.tick_upper)]
    else:
        raise ValueError(ev.kind)
    out = {"address": address, "blockNumber": hex(ev.block), "blockHash": block_hash, "logIndex": hex(ev.log_index),
           "topics": topics, "data": data, "removed": False}
    if block_timestamp is not None:
        out["blockTimestamp"] = hex(block_timestamp)
    return out


# ----------------------------------------------------------------------------- completeness (8.4)
def completeness_check(primary: Transport, secondary: Transport, address: str, blocks: Sequence[int]) -> dict:
    """Swap counts per block on a sample of blocks from two sources; mismatch = hard failure (8.4). A block the second
    source cannot serve (recent-only endpoints) is listed as unavailable, not as a mismatch."""
    mism, unavailable = [], []
    for b in blocks:
        flt = {"address": address, "fromBlock": hex(b), "toBlock": hex(b), "topics": [SWAP_TOPIC]}
        n1 = len(primary.call("eth_getLogs", [flt]))
        try:
            n2 = len(secondary.call("eth_getLogs", [flt]))
        except RpcError as e:
            unavailable.append((b, str(e)[:80]))
            continue
        if n1 != n2:
            mism.append((b, n1, n2))
    return {"sampled": len(blocks), "compared": len(blocks) - len(unavailable), "mismatches": mism,
            "unavailable": unavailable, "ok": not mism}


@dataclass
class PullRecord:
    chain: str
    endpoint: str
    started_utc: str
    finished_utc: str = ""
    cutoff: dict = field(default_factory=dict)
    calls: int = 0
    logs_raw: int = 0
    logs_deduped: int = 0
    headers: int = 0
    formula_check: dict = field(default_factory=dict)
