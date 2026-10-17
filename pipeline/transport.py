"""Live-transport layer with the operator's load rule, shared by every driver that touches a network (stage A and the
confirmatory pulls). Nothing here is called by the analysis modules; tests never instantiate a live transport.

Operator limits (rulings 2026-10-17): below-normal priority; own memory cap 500 MB; free-RAM gate (pause below 3.3 GiB,
resume at 3.5 GiB, checked every minute); public keyless RPC only, throttled; writes only under the project folder.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Sequence

from . import loadguard
from .pull_logs import CurlTransport, RpcError, Transport

PAUSE_BELOW_GIB, RESUME_AT_GIB = 3.3, 3.5
OWN_CAP_MB = 500
GATE_EVERY_S = 60
RETRY_ATTEMPTS = 6
RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "429", "request limit", "ratelimit")
RESULT_CAP_MARKERS = (("more than", "results"), ("too many", "results"), ("exceed", "results"), ("limit", "results"),
                      ("response size",), ("page cap",))
USER_AGENT = "uniswap-pilot/0.1 (research pipeline; keyless public RPC; throttled)"
BLOCKSCOUT_PAGE_CAP = 1000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


def is_rate_limited(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in RATE_LIMIT_MARKERS)


def is_result_cap(msg: str) -> bool:
    m = msg.lower()
    return any(all(k in m for k in combo) for combo in RESULT_CAP_MARKERS)


# ----------------------------------------------------------------------------- counters and the gate
class Stats:
    """Process-wide counters and the operator gate (one driver process at a time)."""
    enabled = True              # tests set False: no RAM gate, no sleeps
    calls = 0
    splits = 0                  # windows split because an endpoint's result cap was exceeded
    band_reads = 0
    bytes_in = 0
    rate_limit_hits = 0
    retries = 0
    fallbacks = 0               # windows served by a fallback source
    pauses: List[dict] = []
    peak_rss_mb = 0.0
    last_gate = 0.0
    lock = threading.Lock()
    gate_lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        cls.calls = cls.splits = cls.band_reads = cls.bytes_in = cls.rate_limit_hits = cls.retries = cls.fallbacks = 0
        cls.pauses, cls.peak_rss_mb, cls.last_gate = [], 0.0, 0.0

    @classmethod
    def snapshot(cls) -> dict:
        return {"calls": cls.calls, "splits": cls.splits, "band_reads": cls.band_reads, "bytes_in_mb": round(cls.bytes_in / 2 ** 20, 1),
                "rate_limit_hits": cls.rate_limit_hits, "retries": cls.retries, "fallbacks": cls.fallbacks,
                "pauses": list(cls.pauses), "peak_rss_mb": round(cls.peak_rss_mb, 1)}

    @classmethod
    def rss_mb(cls) -> float:
        if os.name != "nt":
            try:
                import resource
                return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
            except Exception:  # noqa: BLE001
                return 0.0

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_uint32]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        if not psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            raise SystemExit("ERROR GetProcessMemoryInfo failed; the own-memory cap cannot be enforced")
        return pmc.WorkingSetSize / 2 ** 20

    @classmethod
    def gate(cls, force: bool = False) -> None:
        """Every 60 s: own memory cap, then the free-RAM pause/resume rule."""
        if not cls.enabled:
            return
        with cls.gate_lock:
            now = time.time()
            if not force and now - cls.last_gate < GATE_EVERY_S:
                return
            cls.last_gate = now
            rss = cls.rss_mb()
            cls.peak_rss_mb = max(cls.peak_rss_mb, rss)
            if rss > OWN_CAP_MB:
                raise SystemExit(f"OWN MEMORY CAP EXCEEDED: {rss:.0f} MB > {OWN_CAP_MB} MB")
            free = loadguard.free_ram_gib()
            if free is not None and free < PAUSE_BELOW_GIB:
                t0 = time.time()
                log(f"PAUSE free={free:.2f}GiB < {PAUSE_BELOW_GIB}")
                while True:
                    time.sleep(60)
                    free = loadguard.free_ram_gib()
                    if free is not None and free >= RESUME_AT_GIB:
                        break
                cls.pauses.append({"at": utc_now(), "seconds": int(time.time() - t0)})
                log(f"RESUME free={free:.2f}GiB after {int(time.time() - t0)}s")


# ----------------------------------------------------------------------------- throttle
class RateLimiter:
    """Token-less pacing: at most `rate_per_s` requests per second, shared by every transport that holds it."""

    def __init__(self, rate_per_s: float):
        if rate_per_s <= 0:
            raise ValueError("rate must be positive")
        self.rate_per_s = rate_per_s
        self.interval = 1.0 / rate_per_s
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.interval
        if t > now and Stats.enabled:
            time.sleep(t - now)


# ----------------------------------------------------------------------------- curl helpers
def _curl(args: List[str], timeout_s: int) -> str:
    proc = subprocess.run(["curl", "-sS", "-m", str(timeout_s), "-A", USER_AGENT, *args],
                          capture_output=True, text=True, timeout=timeout_s + 5, check=False)
    with Stats.lock:
        Stats.calls += 1
        Stats.bytes_in += len(proc.stdout)
    if proc.returncode != 0:
        raise RpcError(f"curl rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


class GatedTransport(CurlTransport):
    """CurlTransport with the operator gate, optional pacing, request counting and received-bytes accounting."""

    def __init__(self, url: str, timeout_s: int = 60, pause_s: float = 0.3, limiter: Optional[RateLimiter] = None,
                 label: str = ""):
        super().__init__(url, timeout_s, pause_s)
        self.limiter = limiter
        self.label = label or url.split("//")[-1]

    def _post(self, body: str) -> Any:
        Stats.gate()
        if self.limiter is not None:
            self.limiter.wait()
        out = _curl(["-X", "POST", self.url, "-H", "content-type: application/json", "--data", body], self.timeout_s)
        self.calls += 1
        if Stats.enabled and self.pause_s:
            time.sleep(self.pause_s)
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RpcError(f"non-JSON response from {self.label}: {out[:120]!r}") from e


class BlockscoutTransport(Transport):
    """Blockscout's Etherscan-style `module=logs&action=getLogs`, answered in eth_getLogs shape (Base fallback, 8.2).

    Only eth_getLogs is served. One request per window with no topic filter (the pool's own logs only); topic0 filtering
    is applied client-side. A page of BLOCKSCOUT_PAGE_CAP records is treated as a possible truncation and raised as a
    result-cap error so the caller splits the window. `blockHash` is filled from the block number when the API omits it
    (dedupe then keys on (block, logIndex), which is equivalent below the cutoff)."""

    def __init__(self, api_url: str, timeout_s: int = 60, pause_s: float = 0.5, limiter: Optional[RateLimiter] = None):
        self.api_url, self.timeout_s, self.pause_s, self.limiter = api_url, timeout_s, pause_s, limiter
        self.calls = 0

    @staticmethod
    def _hex(v: Any) -> str:
        s = str(v)
        return s if s.startswith("0x") else hex(int(s))

    def _get(self, url: str) -> Any:
        Stats.gate()
        if self.limiter is not None:
            self.limiter.wait()
        out = _curl(["-H", "accept: application/json", url], self.timeout_s)
        self.calls += 1
        if Stats.enabled and self.pause_s:
            time.sleep(self.pause_s)
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RpcError(f"non-JSON response from Blockscout: {out[:120]!r}") from e

    def call(self, method: str, params: list) -> Any:
        if method != "eth_getLogs":
            raise RpcError(f"Blockscout fallback serves eth_getLogs only, not {method}")
        f = params[0]
        a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        topics = f.get("topics") or []
        t0 = topics[0] if topics else None
        wanted = set(x.lower() for x in t0) if isinstance(t0, list) else ({t0.lower()} if t0 else None)
        q = {"module": "logs", "action": "getLogs", "fromBlock": a, "toBlock": b, "address": f["address"]}
        out = self._get(self.api_url + "?" + urllib.parse.urlencode(q))
        if not isinstance(out, dict):
            raise RpcError(f"Blockscout: unexpected response {str(out)[:120]!r}")
        res = out.get("result")
        if str(out.get("status")) != "1":
            if "no records" in str(out.get("message", "")).lower() or res in ([], None):
                return []
            raise RpcError(f"Blockscout: {str(out)[:200]}")
        if len(res) >= BLOCKSCOUT_PAGE_CAP:
            raise RpcError(f"query returned more than {BLOCKSCOUT_PAGE_CAP - 1} results (Blockscout page cap)")
        logs = []
        for r in res:
            tp = [t for t in r.get("topics", []) if t]
            if not tp or (wanted is not None and tp[0].lower() not in wanted):
                continue
            bn = self._hex(r["blockNumber"])
            logs.append({"address": r["address"], "blockNumber": bn, "blockHash": r.get("blockHash") or ("0x" + format(int(bn, 16), "064x")),
                         "logIndex": self._hex(r["logIndex"]), "topics": tp, "data": r["data"], "removed": False,
                         "blockTimestamp": self._hex(r["timeStamp"]) if r.get("timeStamp") is not None else None})
        return logs


# ----------------------------------------------------------------------------- retry and window splitting
def retry(fn: Callable[[], Any], what: str, attempts: int = RETRY_ATTEMPTS, give_up: str = "exit") -> Any:
    """Transient endpoint errors: back off and retry; rate-limit messages wait longer. give_up: 'exit' | 'raise'."""
    last = ""
    for i in range(attempts):
        try:
            return fn()
        except (RpcError, subprocess.TimeoutExpired) as e:
            last = str(e)
            rl = is_rate_limited(last)
            with Stats.lock:
                Stats.retries += 1
                if rl:
                    Stats.rate_limit_hits += 1
            if is_result_cap(last):
                raise
            wait = min(300, (10 if rl else 5) * 2 ** i)
            log(f"RETRY {what}: {last[:120]} (sleep {wait}s)")
            if Stats.enabled:
                time.sleep(wait)
    if give_up == "raise":
        raise RpcError(f"giving up on {what}: {last[:160]}")
    raise SystemExit(f"ERROR giving up on {what}: {last[:160]}")


def get_logs_split(transport: Transport, address: str, topics: Optional[list], a: int, b: int, depth_left: int = 16) -> list:
    """eth_getLogs over [a, b]; a result-cap refusal splits the window in half and recurses (D3)."""
    flt: dict = {"address": address, "fromBlock": hex(a), "toBlock": hex(b)}
    if topics:
        flt["topics"] = topics
    try:
        return transport.call("eth_getLogs", [flt])
    except RpcError as e:
        if is_result_cap(str(e)) and depth_left > 0 and b > a:
            mid = (a + b) // 2
            with Stats.lock:
                Stats.splits += 1
            return (get_logs_split(transport, address, topics, a, mid, depth_left - 1)
                    + get_logs_split(transport, address, topics, mid + 1, b, depth_left - 1))
        raise


def get_logs_with_fallback(primary: Transport, fallback: Optional[Transport], address: str, topics: Optional[list],
                           a: int, b: int, what: str) -> tuple:
    """(logs, source): the primary with retries; when it gives up and a fallback exists, the fallback with retries."""
    try:
        return retry(lambda: get_logs_split(primary, address, topics, a, b), what, give_up="raise"), "primary"
    except RpcError as e:
        if fallback is None:
            raise SystemExit(f"ERROR {what}: {str(e)[:160]}")
        log(f"FALLBACK {what}: primary gave up ({str(e)[:80]}); using the fallback source")
        with Stats.lock:
            Stats.fallbacks += 1
        return retry(lambda: get_logs_split(fallback, address, topics, a, b), what + " (fallback)"), "fallback"
