"""Guarded launcher for the frozen analysis runner (operator ruling 20 Oct 2026): runs `python -m pipeline.run_analysis`
as a child at BELOW_NORMAL priority, samples the child's working set every 2 s (peak recorded), and if free RAM drops
below the 1.5 GiB hard floor suspends the child (NtSuspendProcess), logs the event, and resumes it (NtResumeProcess)
once free RAM is back at RESUME_GIB. No frozen code is touched; the runner's own arguments pass through unchanged.
Record: data/confirmatory/logs/analysis_run_record.json (peak_rss_mb, pauses, exit code, duration).
Usage: python tools/run_analysis_guarded.py [runner args...]   |   python tools/run_analysis_guarded.py --self-test
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import loadguard  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe"
FLOOR_GIB, RESUME_GIB = 1.5, 2.0
SAMPLE_S = 2.0
LOG = ROOT / "data" / "confirmatory" / "logs" / "analysis.log"
RECORD = ROOT / "data" / "confirmatory" / "logs" / "analysis_run_record.json"
BELOW_NORMAL_PRIORITY_CLASS = 0x4000


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def working_set_mb(handle: int) -> tuple:
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_uint32]
    if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(pmc), pmc.cb):
        return (0.0, 0.0)
    return (pmc.WorkingSetSize / 2 ** 20, pmc.PeakWorkingSetSize / 2 ** 20)


TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SUSPEND_RESUME = 0x1000, 0x0800


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_char * 260)]


def process_tree(root_pid: int) -> list:
    """root_pid and all its descendants (the venv python.exe is a launcher stub whose child is the real interpreter)."""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32First.argtypes = k.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    parents = {}
    try:
        e = PROCESSENTRY32(); e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            parents.setdefault(int(e.th32ParentProcessID), []).append(int(e.th32ProcessID))
            ok = k.Process32Next(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(ctypes.c_void_p(snap))
    out, todo = [], [root_pid]
    while todo:
        pid = todo.pop(0)
        out.append(pid)
        todo.extend(c for c in parents.get(pid, []) if c not in out)
    return out


def with_handle(pid: int, fn):
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return None
    try:
        return fn(h)
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


def tree_working_set_mb(root_pid: int) -> float:
    return sum((with_handle(pid, working_set_mb) or (0.0, 0.0))[0] for pid in process_tree(root_pid))


def suspend_tree(root_pid: int) -> int:
    pids = process_tree(root_pid)
    return sum(1 for pid in reversed(pids) if with_handle(pid, lambda h: ctypes.windll.ntdll.NtSuspendProcess(ctypes.c_void_p(h)) == 0))


def resume_tree(root_pid: int) -> int:
    pids = process_tree(root_pid)
    return sum(1 for pid in pids if with_handle(pid, lambda h: ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(h)) == 0))


def run(cmd: list, label: str) -> dict:
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), creationflags=BELOW_NORMAL_PRIORITY_CLASS)
    log(f"{label}: started pid {proc.pid} at below-normal priority: {' '.join(cmd)}")
    peak = 0.0
    pauses = []
    paused_at = None
    lowest = None
    while proc.poll() is None:
        ws = tree_working_set_mb(proc.pid)
        peak = max(peak, ws)
        free = loadguard.free_ram_gib()
        if free is not None:
            lowest = free if lowest is None else min(lowest, free)
            if paused_at is None and free < FLOOR_GIB:
                ok = suspend_tree(proc.pid)
                paused_at = time.time()
                log(f"HARD FLOOR free={free:.2f}GiB < {FLOOR_GIB}: processes suspended={ok}; waiting for >= {RESUME_GIB} GiB (report to the operator)")
            elif paused_at is not None and free >= RESUME_GIB:
                ok = resume_tree(proc.pid)
                pauses.append({"at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": int(time.time() - paused_at), "resumed_at_gib": round(free, 2)})
                log(f"HARD FLOOR RESUME free={free:.2f}GiB after {int(time.time() - paused_at)}s: processes resumed={ok}")
                paused_at = None
        time.sleep(SAMPLE_S)
    if paused_at is not None:
        pauses.append({"at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": int(time.time() - paused_at), "resumed_at_gib": None})
    rec = {"label": label, "cmd": cmd, "exit": proc.returncode, "seconds": round(time.time() - t0, 1), "peak_rss_mb": round(peak, 1),
           "lowest_free_gib": None if lowest is None else round(lowest, 2), "pauses": pauses, "floor_gib": FLOOR_GIB, "resume_gib": RESUME_GIB,
           "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    log(f"{label}: exit {proc.returncode} after {rec['seconds']}s, peak working set {peak:.1f} MB, lowest free RAM {rec['lowest_free_gib']} GiB, pauses {len(pauses)}")
    return rec


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--self-test"]:
        # a dummy child that allocates ~60 MB and sleeps; the floor is forced to trip once to exercise suspend/resume
        global FLOOR_GIB, RESUME_GIB
        child = [str(PY), "-c", "import time; b = bytearray(60 * 2**20); time.sleep(6); print('child done')"]
        FLOOR_GIB, RESUME_GIB = 10 ** 6, 0.0          # trips at once, resumes at once
        rec = run(child, "self-test")
        assert rec["exit"] == 0 and rec["peak_rss_mb"] > 50 and len(rec["pauses"]) >= 1, rec
        print("SELF-TEST OK", json.dumps(rec))
        return
    cmd = [str(PY), "-m", "pipeline.run_analysis"] + args
    rec = run(cmd, "analysis")
    RECORD.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    sys.exit(rec["exit"])


if __name__ == "__main__":
    main()
