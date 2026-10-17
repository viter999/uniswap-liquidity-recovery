"""freeze_hash.py -- compute freeze-manifest hashes (prereg v0.3 section 10.3).

For every manifest file prints sha256 of the bytes as stored and sha256 of the LF-normalised content (CRLF -> LF only).
Output is Markdown, ready to paste into RUNLOG.md. Stdlib only.

Usage (from the uniswap-pilot folder):
  python -I tools/freeze_hash.py           prereg-freeze manifest (the fixed list below)
  python -I tools/freeze_hash.py --code    CODE FREEZE manifest: every file under pipeline/ and tests/ (no __pycache__,
                                           no *.pyc) plus requirements.lock (ruling 17 Oct 2026: full-tree hashing)
  python -I tools/freeze_hash.py --verify MANIFEST.md   re-hash the files listed in a manifest and report differences
"""
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MANIFEST = [
    "PREREG_uniswap_recovery_v0.3.md",
    "outages/outage_list_20261017.md",
    "probes/probe_rpc.py",
    "probes/probe_rpc_alt.py",
    "probes/probe_20261017.json",
    "probes/probe_alt_20261017.json",
    "probes/probe_drpc_20261017.txt",
    "probes/probe_genesis_20261017.txt",
    "proxy/proxy_episode_count.py",
    "proxy/proxy_summary.json",
    "bigquery/feasibility_episode_count.sql",
]
CODE_DIRS = ("pipeline", "tests")
CODE_EXTRA = ("requirements.lock",)
ROW = re.compile(r"^\| `([^`]+)` \| (\d+) \| `([0-9a-f]{64})` \| `([0-9a-f]{64})` \|$", re.M)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def code_manifest() -> list:
    files = []
    for d in CODE_DIRS:
        for p in sorted((BASE / d).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                files.append(p.relative_to(BASE).as_posix())
    return files + list(CODE_EXTRA)


def table(files: list) -> str:
    lines = ["| file | bytes | sha256 (as stored) | sha256 (LF-normalised) |", "|---|---|---|---|"]
    for m in files:
        raw = (BASE / m).read_bytes()
        lf = raw.replace(b"\r\n", b"\n")
        lines.append(f"| `{m}` | {len(raw)} | `{sha256_bytes(raw)}` | `{sha256_bytes(lf)}` |")
    return "\n".join(lines)


def verify(manifest_path: Path) -> int:
    rows = ROW.findall(manifest_path.read_text(encoding="utf-8"))
    bad = 0
    for f, size, stored, lf in rows:
        p = BASE / f
        if not p.is_file():
            print(f"MISSING {f}")
            bad += 1
            continue
        raw = p.read_bytes()
        ok_stored = sha256_bytes(raw) == stored
        ok_lf = sha256_bytes(raw.replace(b"\r\n", b"\n")) == lf
        if not (ok_stored and ok_lf):
            print(f"CHANGED {f} (stored {'ok' if ok_stored else 'DIFF'}, LF {'ok' if ok_lf else 'DIFF'})")
            bad += 1
    print(f"verified {len(rows)} files, {bad} problems")
    return 1 if bad else 0


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--verify"]:
        return verify(Path(args[1]))
    files = code_manifest() if args[:1] == ["--code"] else MANIFEST
    missing = [m for m in files if not (BASE / m).is_file()]
    if missing:
        print("MISSING: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(table(files))
    print(f"\nHashed {len(files)} files at {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
