"""LOAD RULE wrapper (operator ruling 19 Oct 2026, deviation D15). Runs one pilot script in-process with the RAM gate
lowered to START 2.0 / PAUSE 2.2 / RESUME 2.4 GiB and a hard floor of 1.5 GiB, without editing the frozen
pipeline/loadguard.py (MIN_FREE_GIB 3.0) or pipeline/transport.py (PAUSE_BELOW_GIB 3.3 / RESUME_AT_GIB 3.5): the module
constants are set on the imported modules before the script runs, so the frozen apply() and Stats.gate() enforce the lower
values and log them. Scope (ruling): the official-header probe and the Arbitrum Tenderly window pull only; the Arbitrum
depth driver and the analysis run under the normal gate unless their measured peak RAM is at most about 500 MB.
Hard floor: a watchdog thread samples free RAM every 10 s; below 1.5 GiB it takes the frozen request gate lock, so the
main thread pauses at its next Stats.gate() call (at most one chunk later), holds it until free RAM is back at RESUME,
and appends the event to data/confirmatory/logs/hard_floor_events.jsonl (report). The own-process cap (500 MB) and
below-normal priority stay as frozen.
Usage: python tools/loadrule.py [--gate START,PAUSE,RESUME] <script.py> [args...]   |   python tools/loadrule.py [--gate ...] --show
Ruling 20 Oct 2026 (D15 amendment): --gate 1.7,1.8,2.0 for the Arbitrum depth driver; the hard floor stays 1.5 GiB and
is not an argument. --own-cap MB (ruling 20 Oct): a watchdog stops the run with a report line and a clean exit 0 (so the
supervisor does not restart it) if this process's own working set exceeds MB; the frozen 500 MB cap stays in place.
"""
from __future__ import annotations

import hashlib
import json
import runpy
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import loadguard, transport  # noqa: E402

START_GIB, PAUSE_GIB, RESUME_GIB, FLOOR_GIB = 2.0, 2.2, 2.4, 1.5
EVENTS = ROOT / "data" / "confirmatory" / "logs" / "hard_floor_events.jsonl"
SAMPLE_S = 10
OWN_CAP_MB = None          # --own-cap: stop-and-report threshold for this process's working set (ruling 20 Oct 2026)
OWN_STOP = ROOT / "data" / "confirmatory" / "logs" / "own_memory_stop.json"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def apply_thresholds() -> None:
    loadguard.MIN_FREE_GIB = START_GIB
    transport.PAUSE_BELOW_GIB = PAUSE_GIB
    transport.RESUME_AT_GIB = RESUME_GIB


def floor_watch(script: str) -> None:
    while True:
        if OWN_CAP_MB is not None:
            rss = transport.Stats.rss_mb()
            if rss > OWN_CAP_MB:
                ev = {"at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "script": script, "rss_mb": round(rss, 1), "cap_mb": OWN_CAP_MB}
                OWN_STOP.parent.mkdir(parents=True, exist_ok=True)
                with OWN_STOP.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev) + "\n")
                transport.log(f"OWN MEMORY RULE STOP: rss {rss:.0f} MB > {OWN_CAP_MB} MB; stopping this run (clean exit, no restart) - report to the operator")
                import os
                os._exit(0)
        free = loadguard.free_ram_gib()
        if free is not None and free < FLOOR_GIB:
            t0 = time.time()
            low = free
            transport.log(f"HARD FLOOR free={free:.2f}GiB < {FLOOR_GIB}: pausing at the next request gate until >= {RESUME_GIB}")
            with transport.Stats.gate_lock:
                while True:
                    time.sleep(SAMPLE_S)
                    free = loadguard.free_ram_gib()
                    if free is not None:
                        low = min(low, free)
                        if free >= RESUME_GIB:
                            break
            ev = {"at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "script": script,
                  "lowest_free_gib": round(low, 2), "paused_s": int(time.time() - t0), "resumed_at_gib": round(free, 2)}
            EVENTS.parent.mkdir(parents=True, exist_ok=True)
            with EVENTS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
            transport.log(f"HARD FLOOR RESUME free={free:.2f}GiB after {ev['paused_s']}s (lowest {low:.2f}GiB)")
        time.sleep(SAMPLE_S)


def main() -> None:
    global START_GIB, PAUSE_GIB, RESUME_GIB
    if len(sys.argv) >= 3 and sys.argv[1] == "--gate":
        START_GIB, PAUSE_GIB, RESUME_GIB = (float(x) for x in sys.argv[2].split(","))
        if not (FLOOR_GIB <= START_GIB <= PAUSE_GIB <= RESUME_GIB):
            raise SystemExit(f"REFUSED: gate {START_GIB}/{PAUSE_GIB}/{RESUME_GIB} must satisfy floor {FLOOR_GIB} <= start <= pause <= resume")
        del sys.argv[1:3]
    global OWN_CAP_MB
    if len(sys.argv) >= 3 and sys.argv[1] == "--own-cap":
        OWN_CAP_MB = float(sys.argv[2])
        del sys.argv[1:3]
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    apply_thresholds()
    if sys.argv[1] == "--show":
        print(json.dumps({"wrapper_sha256": sha256(Path(__file__)), "start_gib": loadguard.MIN_FREE_GIB,
                          "pause_gib": transport.PAUSE_BELOW_GIB, "resume_gib": transport.RESUME_AT_GIB,
                          "floor_gib": FLOOR_GIB, "frozen_own_cap_mb": transport.OWN_CAP_MB, "wrapper_own_cap_mb": OWN_CAP_MB, "free_gib_now": round(loadguard.free_ram_gib() or -1, 2)}))
        return
    script = str(Path(sys.argv[1]).resolve())
    transport.log(f"loadrule wrapper sha256 {sha256(Path(__file__))}: start {START_GIB} / pause {PAUSE_GIB} / resume {RESUME_GIB} / "
                  f"floor {FLOOR_GIB} GiB, wrapper own cap {OWN_CAP_MB} MB (D15) for {Path(script).name}")
    threading.Thread(target=floor_watch, args=(Path(script).name,), daemon=True, name="hard-floor").start()
    sys.argv = [script] + sys.argv[2:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
