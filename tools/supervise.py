"""Process-level supervisor for the frozen, resumable drivers (operational; no frozen-code change).

The frozen drivers retry transient endpoint errors inside most request paths, but not around the Ethereum blockTimestamp
header cross-check (price_only._chunk_timestamps, window_pull._timestamps): a single HTTP 504 there ends the process.
Every phase is resumable from its chunk, window and record files, so this supervisor relaunches the same command after a
pause when it exits non-zero, up to MAX_ATTEMPTS times, and records every attempt in <log>.attempts.json.
Usage: python tools/supervise.py <log-file> -- <command ...>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_ATTEMPTS = 480          # 480 x 90 s = 12 h of waiting while the load rule refuses to start (free RAM < 3.0 GiB)
PAUSE_S = 90


def main() -> int:
    if "--" not in sys.argv:
        raise SystemExit("usage: supervise.py <log-file> -- <command ...>")
    i = sys.argv.index("--")
    log_path = Path(sys.argv[1])
    cmd = sys.argv[i + 1:]
    root = Path(__file__).resolve().parent.parent
    if not Path(cmd[0]).is_absolute() and (root / cmd[0]).exists():      # Windows resolves relative executables against the parent, not cwd
        cmd[0] = str(root / cmd[0])
    attempts = []
    for n in range(1, MAX_ATTEMPTS + 1):
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"=== supervisor attempt {n} at {started}: {' '.join(cmd)}\n")
            fh.flush()
            rc = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(Path(__file__).resolve().parent.parent))
        attempts.append({"attempt": n, "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "exit": rc})
        Path(str(log_path) + ".attempts.json").write_text(json.dumps(attempts, indent=1), encoding="utf-8")
        if rc == 0:
            return 0
        if n < MAX_ATTEMPTS:
            time.sleep(PAUSE_S)
    return 1


if __name__ == "__main__":
    sys.exit(main())
