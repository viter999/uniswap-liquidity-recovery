"""Verify that the `blockTimestamp` field inside eth_getLogs results equals the block header timestamp exactly.

Ethereum: 1,000 random log-bearing 2023 blocks (from data/stageA/headers.npz) -> one eth_getLogs per block (0.05% pool)
on rpc.mevblocker.io; compare log.blockTimestamp with the stored header timestamp.
Arbitrum: blocks of ~200 logs from one range on arb1.arbitrum.io/rpc; compare with single eth_getBlockByNumber headers.
Unichain: logs from one range on mainnet.unichain.org; compare with the OP-stack formula (genesis + n) and 10 headers.
Base: logs from one range on base.drpc.org (if it returns blockTimestamp); compare with the OP-stack formula.

Operator load rule: below-normal priority; pause when free RAM < 3.3 GiB, resume at 3.5 GiB; checked every minute.
Read-only against the chains; nothing written except the result JSON. No 2024-2026 Ethereum data is used
(the Ethereum sample is 2023; the Arbitrum/Unichain/Base ranges are block ranges used only for this timestamp check).
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import loadguard  # noqa: E402
from pipeline.common import SWAP_TOPIC, timestamp_from_block  # noqa: E402

SEED = 20261017
OUT = ROOT / "data" / "stageA" / "blocktimestamp_check.json"
PAUSE_BELOW, RESUME_AT = 3.3, 3.5
last_gate = [0.0]
pauses = []


def gate():
    now = time.time()
    if now - last_gate[0] < 60:
        return
    last_gate[0] = now
    free = loadguard.free_ram_gib()
    if free is not None and free < PAUSE_BELOW:
        t0 = time.time()
        print(f"PAUSE free={free:.2f}", flush=True)
        while True:
            time.sleep(60)
            free = loadguard.free_ram_gib()
            if free is not None and free >= RESUME_AT:
                break
        pauses.append(int(time.time() - t0))
        print(f"RESUME after {pauses[-1]}s", flush=True)


def post(url, method, params, timeout=60):
    gate()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    for attempt in range(5):
        p = subprocess.run(["curl", "-sS", "-m", str(timeout), "-X", "POST", url, "-H", "content-type: application/json",
                            "--data", body], capture_output=True, text=True, check=False)
        time.sleep(0.3)
        try:
            d = json.loads(p.stdout)
            if "result" in d:
                return d["result"]
            err = d.get("error")
        except json.JSONDecodeError:
            err = p.stdout[:80] or p.stderr[:80]
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{method} failed: {err}")


def check_ethereum() -> dict:
    z = np.load(ROOT / "data" / "stageA" / "headers.npz")
    blocks, ts = z["blocks"], z["timestamps"]
    rng = random.Random(SEED)
    idx = sorted(rng.sample(range(len(blocks)), 1000))
    mism, no_field, no_logs, n = [], 0, 0, 0
    pool = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
    for k, i in enumerate(idx):
        b = int(blocks[i])
        logs = post("https://rpc.mevblocker.io", "eth_getLogs", [{"address": pool, "fromBlock": hex(b), "toBlock": hex(b)}])
        if not logs:
            no_logs += 1
            continue
        for lg in logs:
            if "blockTimestamp" not in lg:
                no_field += 1
                break
            n += 1
            if int(lg["blockTimestamp"], 16) != int(ts[i]):
                mism.append((b, int(lg["blockTimestamp"], 16), int(ts[i])))
            break
        if (k + 1) % 100 == 0:
            print(f"ethereum {k + 1}/1000 compared={n} mismatches={len(mism)}", flush=True)
    return {"sampled_blocks": len(idx), "compared": n, "blocks_without_logs": no_logs, "logs_without_field": no_field,
            "mismatches": mism[:20], "n_mismatches": len(mism)}


def check_arbitrum() -> dict:
    url = "https://arb1.arbitrum.io/rpc"
    pool = "0xC6962004f452bE9203591991D15f6b388e09E8D0"
    logs = post(url, "eth_getLogs", [{"address": pool, "fromBlock": hex(165_000_000), "toBlock": hex(165_050_000), "topics": [SWAP_TOPIC]}])
    by_block = {}
    for lg in logs:
        by_block.setdefault(int(lg["blockNumber"], 16), int(lg["blockTimestamp"], 16))
    rng = random.Random(SEED)
    sample = sorted(rng.sample(sorted(by_block), min(200, len(by_block))))
    mism = []
    for k, b in enumerate(sample):
        blk = post(url, "eth_getBlockByNumber", [hex(b), False])
        if int(blk["timestamp"], 16) != by_block[b]:
            mism.append((b, by_block[b], int(blk["timestamp"], 16)))
        if (k + 1) % 50 == 0:
            print(f"arbitrum {k + 1}/{len(sample)} mismatches={len(mism)}", flush=True)
    return {"logs": len(logs), "distinct_blocks": len(by_block), "compared": len(sample), "mismatches": mism[:20], "n_mismatches": len(mism)}


def check_opstack(name: str, url: str, pool: str, a: int, b: int, n_headers: int = 10) -> dict:
    logs = post(url, "eth_getLogs", [{"address": pool, "fromBlock": hex(a), "toBlock": hex(b), "topics": [SWAP_TOPIC]}])
    if not logs:
        return {"logs": 0, "note": "no logs in range"}
    if "blockTimestamp" not in logs[0]:
        return {"logs": len(logs), "field_present": False}
    by_block = {}
    for lg in logs:
        by_block.setdefault(int(lg["blockNumber"], 16), int(lg["blockTimestamp"], 16))
    formula_mism = [(blk, t, timestamp_from_block(name, blk)) for blk, t in by_block.items() if t != timestamp_from_block(name, blk)]
    rng = random.Random(SEED)
    sample = sorted(rng.sample(sorted(by_block), min(n_headers, len(by_block))))
    hdr_mism = []
    for blk in sample:
        h = post(url, "eth_getBlockByNumber", [hex(blk), False])
        if int(h["timestamp"], 16) != by_block[blk]:
            hdr_mism.append((blk, by_block[blk], int(h["timestamp"], 16)))
    return {"logs": len(logs), "distinct_blocks": len(by_block), "field_present": True,
            "formula_mismatches": formula_mism[:10], "n_formula_mismatches": len(formula_mism),
            "headers_compared": len(sample), "header_mismatches": hdr_mism}


def main():
    loadguard.set_below_normal_priority()
    gate()
    t0 = time.time()
    res = {"seed": SEED}
    print("PHASE ethereum", flush=True)
    res["ethereum"] = check_ethereum()
    print("PHASE arbitrum", flush=True)
    res["arbitrum"] = check_arbitrum()
    print("PHASE unichain", flush=True)
    res["unichain"] = check_opstack("unichain", "https://mainnet.unichain.org", "0x8927058918e3cff6f55efe45a58db1be1f069e49", 46_440_983, 46_450_983)
    print("PHASE base", flush=True)
    res["base"] = check_opstack("base", "https://base.drpc.org", "0x6c561B446416E1A00E8E93E221854d6eA4171372", 52_600_000, 52_600_099)
    res["pauses_s"] = pauses
    res["elapsed_s"] = int(time.time() - t0)
    OUT.write_text(json.dumps(res, indent=2))
    print("DONE " + json.dumps({k: (v.get("n_mismatches", v.get("n_formula_mismatches")) if isinstance(v, dict) else v) for k, v in res.items()}), flush=True)


if __name__ == "__main__":
    main()
