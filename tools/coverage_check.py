"""Coverage check of the lifetime Mint/Burn chunk ranges per R1 pool (operator ruling 18 Oct 2026): the chunk files must
form one continuous block range from the pool's creation block (or earlier) to at least the latest window start b0 that
the R1 route needs, with no gap and no overlap; the chain cutoff is reported beside it. Also checks every pulled window's
(b0, b_end] coverage and the row order inside each chunk. Read-only. Output: data/confirmatory/depth/coverage_<chain>.json
Usage: python tools/coverage_check.py [--chain ethereum,arbitrum]
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.common import POOLS  # noqa: E402
from pipeline.window_pull import pool_dir_name  # noqa: E402

DEPTH = ROOT / "data" / "confirmatory" / "depth"
STAGE_A = ROOT / "data" / "stageA"
RANGE = re.compile(r"(\d{9,10})-(\d{9,10})\.csv$")


def chunk_ranges(dirs):
    out = []
    for d in dirs:
        for f in sorted(glob.glob(str(Path(d) / "*.csv"))):
            m = RANGE.search(os.path.basename(f))
            if m:
                out.append((int(m.group(1)), int(m.group(2)), f))
    return sorted(out)


def check_pool(chain: str, key: str, creation: int, need_to: int) -> dict:
    pool = POOLS[key]
    dirs = [DEPTH / "lifetime" / pool_dir_name(pool) / "mintburn"]
    if chain == "ethereum":
        dirs.insert(0, STAGE_A / "logs" / pool_dir_name(pool) / "mintburn")
    ranges = chunk_ranges(dirs)
    gaps, overlaps = [], []
    for (a0, b0, f0), (a1, b1, f1) in zip(ranges, ranges[1:]):
        if a1 > b0 + 1:
            gaps.append((b0 + 1, a1 - 1))
        elif a1 <= b0:
            overlaps.append((a1, min(b0, b1), os.path.basename(f0), os.path.basename(f1)))
    first, last = (ranges[0][0], ranges[-1][1]) if ranges else (None, None)
    # row order inside the chunks and block membership
    bad_rows = 0
    for a, b, f in ranges:
        prev = None
        with open(f, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                blk, idx = int(row["block"]), int(row["log_index"])
                if not (a <= blk <= b) or (prev is not None and (blk, idx) <= prev):
                    bad_rows += 1
                prev = (blk, idx)
    ok = bool(ranges) and first <= creation and not gaps and not overlaps and last >= need_to and bad_rows == 0
    return {"pool": key, "chunks": len(ranges), "first_block": first, "last_block": last, "creation_block": creation,
            "needed_to_block": need_to, "covers_creation": first is not None and first <= creation, "reaches_needed": last is not None and last >= need_to,
            "gaps": gaps, "overlaps": overlaps, "rows_out_of_order_or_outside_chunk": bad_rows, "pass": ok}


def check_windows(chain: str) -> dict:
    out = {"windows": 0, "incomplete": [], "b0_mismatch": []}
    pre = DEPTH / f"prefetch_windows_{chain}.json"
    wins = json.loads(pre.read_text(encoding="utf-8"))["windows"] if pre.exists() else {}
    for meta in glob.glob(str(DEPTH / "windows" / f"{chain}_*" / "ep*" / "meta.json")):
        m = json.loads(Path(meta).read_text(encoding="utf-8"))
        out["windows"] += 1
        if not m.get("complete"):
            out["incomplete"].append(meta)
        w = wins.get(str(m["episode_id"]))
        if w and (m["b0"], m["b_end"]) != tuple(w):
            out["b0_mismatch"].append((m["pool_key"], m["episode_id"], (m["b0"], m["b_end"]), w))
    out["pass"] = not out["incomplete"] and not out["b0_mismatch"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="ethereum,arbitrum")
    args = ap.parse_args(argv)
    all_ok = True
    for chain in args.chain.split(","):
        cut = json.loads((DEPTH / f"cutoff_{chain}.json").read_text(encoding="utf-8"))["block"]
        recs = [json.loads(l) for l in (DEPTH / f"results_{chain}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
            if (DEPTH / f"results_{chain}.jsonl").exists() else []
        pre = DEPTH / f"prefetch_windows_{chain}.json"
        if pre.exists():
            need_to = max(b0 for b0, _ in json.loads(pre.read_text(encoding="utf-8"))["windows"].values())
        else:
            need_to = max((r["b0"] for r in recs if r.get("b0") is not None), default=0)
        report = {"chain": chain, "cutoff_block": cut, "needed_to_block (latest window b0)": need_to, "pools": [], "windows": check_windows(chain)}
        for key in [p.key for p in POOLS.values() if p.chain == chain]:
            cf = DEPTH / f"creation_{pool_dir_name(POOLS[key])}.json"
            creation = json.loads(cf.read_text(encoding="utf-8"))["creation_block"] if cf.exists() else None
            if creation is None:
                report["pools"].append({"pool": key, "pass": False, "error": "no creation file"})
                all_ok = False
                continue
            r = check_pool(chain, key, creation, need_to)
            report["pools"].append(r)
            all_ok = all_ok and r["pass"]
        report["pass"] = all(p["pass"] for p in report["pools"]) and report["windows"]["pass"]
        all_ok = all_ok and report["pass"]
        (DEPTH / f"coverage_{chain}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "pools"}))
        for p in report["pools"]:
            print("  ", {k: v for k, v in p.items() if k in ("pool", "chunks", "first_block", "last_block", "creation_block", "needed_to_block", "gaps", "overlaps", "rows_out_of_order_or_outside_chunk", "pass")})
    print("COVERAGE", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
