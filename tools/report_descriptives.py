"""Read-only descriptives for the pilot report (operator ruling 21 Oct 2026).

Reads ONLY existing run outputs under data/confirmatory/ (decision.json, results_<chain>.jsonl, the window
meta.json files) and writes one JSON and one Markdown file under drafts/. It runs no pipeline code, computes no
test and no p-value, and changes no verdict. Pair validity and d_e^val follow prereg v0.3 sections 7.1a, 7.2-7.4
exactly so that the reconstruction can be reconciled against confirmatory_tests.json before any number is used.

Items and their status:
  pre-registered (v0.3 7.6 / 7.8 / 8.4): observed SD of d_e; per-chain H1 and per-tier H2 distributions; up vs
    down; pre-shock depth covariate; tier contrast pre/post fee switch; chain_degraded / header_gap episodes;
    log counts before/after dedupe.
  "descriptive, not pre-registered": SD of d_e^rank; medians of T_rec over uncensored pool-episodes only;
    the D_pre split used to present the covariate; sha256 of the run outputs not already in RUNLOG.

Usage (from the project folder):  python -I tools/report_descriptives.py
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "data" / "confirmatory"
OUT_JSON = ROOT / "drafts" / "REPORT_DESCRIPTIVES_20261021.json"
OUT_MD = ROOT / "drafts" / "REPORT_DESCRIPTIVES_20261021.md"

CONF_CHAINS = ("ethereum", "arbitrum", "base")
TIERS = ("0.05%", "0.3%")
FEE_SWITCH_MIN = int(datetime(2025, 12, 28, tzinfo=timezone.utc).timestamp()) // 60  # prereg 10.2 S2, applied to t0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_results() -> dict:
    res = {}
    for chain in CONF_CHAINS + ("unichain",):
        with open(CONF / "depth" / f"results_{chain}.jsonl", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    res[(r["pool_key"], r["episode_id"])] = r
    return res


def member_ok(r: dict) -> bool:
    """7.1a membership conditions except the both-censored rule."""
    return bool(r.get("dip")) and bool(r.get("local_shock")) and not r.get("outage") \
        and not r.get("band_read_failed") and not r.get("pool_absent") and r.get("status") == "ok"


def pair_r(num: dict, den: dict):
    """log((T_rec(num)+1)/(T_rec(den)+1)); None if the pair is invalid (7.1a)."""
    if not (member_ok(num) and member_ok(den)):
        return None
    if num.get("censored") and den.get("censored"):
        return None
    return math.log((num["t_rec"] + 1.0) / (den["t_rec"] + 1.0)), bool(num.get("censored") or den.get("censored"))


def episode_stats(pairs):
    """pairs: list of (r, one_censored). Returns (d_val, d_rank_or_None, n_pairs, n_uncensored)."""
    if not pairs:
        return None
    d_val = st.fmean(p[0] for p in pairs)
    unc = [p[0] for p in pairs if not p[1]]
    d_rank = st.fmean(unc) if unc else None  # rank-only replacement handled by the caller
    return d_val, d_rank, len(pairs), len(unc)


def sd(xs):
    return st.stdev(xs) if len(xs) > 1 else None


def quartiles(xs):
    xs = sorted(xs)
    if not xs:
        return None
    q = st.quantiles(xs, n=4, method="inclusive") if len(xs) > 1 else [xs[0]] * 3
    return {"n": len(xs), "min": xs[0], "q1": q[0], "median": st.median(xs), "q3": q[2], "max": xs[-1]}


def main() -> int:
    decision = json.load(open(CONF / "price_only" / "decision.json", encoding="utf-8"))
    episodes = decision["sets"][decision["decision"]["chosen"]]["episodes"]
    res = load_results()
    out = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "inputs": {"decision.json": sha256(CONF / "price_only" / "decision.json")},
           "rules": "prereg v0.3 7.1a, 7.2, 7.3, 7.4 (d_e^val over all valid pairs, censored members at value)",
           "tests": {}, "per_stratum": {}, "direction": {}, "fee_switch": {}, "covariate": {},
           "flagged_episodes": {}, "dedupe": {}, "hashes": {}, "labels": {}}
    for chain in CONF_CHAINS + ("unichain",):
        out["inputs"][f"results_{chain}.jsonl"] = sha256(CONF / "depth" / f"results_{chain}.jsonl")

    # ---- per-episode statistics for the three tests (reconstruction, to be reconciled) ----
    tests = {
        "H1": [("chain", c, (f"{c}:0.3%", f"{c}:0.05%")) for c in CONF_CHAINS],
        "H2-arbitrum": [("tier", t, (f"ethereum:{t}", f"arbitrum:{t}")) for t in TIERS],
        "H2-base": [("tier", t, (f"ethereum:{t}", f"base:{t}")) for t in TIERS],
    }
    per_episode = {}
    for name, specs in tests.items():
        d_vals, d_ranks_val, rows = [], [], []
        stratum_pairs = defaultdict(list)
        for e in episodes:
            ep = e["episode_id"]
            pairs = []
            for _, stratum, (num_key, den_key) in specs:
                pr = pair_r(res[(num_key, ep)], res[(den_key, ep)])
                if pr is not None:
                    pairs.append(pr)
                    stratum_pairs[stratum].append(pr[0])
            es = episode_stats(pairs)
            if es is None:
                continue
            d_val, d_rank, n_pairs, n_unc = es
            rows.append({"episode_id": ep, "direction": e["direction"], "t0": e["t0"], "d_val": d_val,
                         "d_rank_uncensored": d_rank, "pairs": n_pairs, "uncensored_pairs": n_unc})
            d_vals.append(d_val)
        # rank-only replacement (7.4): sign(d_val) * (max |d_rank| over uncensored episodes + 1), fixed from the full sample
        max_abs = max(abs(r["d_rank_uncensored"]) for r in rows if r["d_rank_uncensored"] is not None)
        d_ranks = []
        for r in rows:
            if r["d_rank_uncensored"] is not None:
                r["d_rank"] = r["d_rank_uncensored"]
            else:
                s = (r["d_val"] > 0) - (r["d_val"] < 0)
                r["d_rank"] = s * (max_abs + 1.0)
            if r["d_rank"] != 0:
                d_ranks.append(r["d_rank"])
        med = st.median(d_vals)
        out["tests"][name] = {
            "episodes_with_valid_pair": len(rows),
            "median_ratio_reconstructed": math.exp(med),
            "n_rank_nonzero": len(d_ranks),
            "sd_d_val": sd(d_vals),
            "sd_d_rank_nonzero": sd(d_ranks),
            "d_val_quartiles": quartiles(d_vals),
            "per_episode": rows,
        }
        out["per_stratum"][name] = {s: {"pairs_valid": len(v), "median_r": st.median(v),
                                        "median_ratio": math.exp(st.median(v)), "quartiles": quartiles(v)}
                                    for s, v in stratum_pairs.items()}
        out["direction"][name] = {d: {"n": len(v), "median_d_val": st.median(v), "median_ratio": math.exp(st.median(v))}
                                  for d, v in {d: [r["d_val"] for r in rows if r["direction"] == d]
                                               for d in ("ETH_down", "ETH_up")}.items() if v}
        pre = [r["d_val"] for r in rows if r["t0"] < FEE_SWITCH_MIN]
        post = [r["d_val"] for r in rows if r["t0"] >= FEE_SWITCH_MIN]
        out["fee_switch"][name] = {"pre_2025-12-28": {"n": len(pre), "median_ratio": math.exp(st.median(pre)) if pre else None},
                                   "post_2025-12-28": {"n": len(post), "median_ratio": math.exp(st.median(post)) if post else None}}

    # ---- pre-shock depth covariate: per pool, D_pre quartiles and T_rec medians below/above the pool's median D_pre ----
    for pool in sorted({k[0] for k in res}):
        rs = [r for k, r in res.items() if k[0] == pool and member_ok(r)]
        if not rs:
            continue
        dpre = [r["d_pre"] for r in rs]
        m = st.median(dpre)
        lo = [r["t_rec"] for r in rs if r["d_pre"] <= m]
        hi = [r["t_rec"] for r in rs if r["d_pre"] > m]
        out["covariate"][pool] = {"n": len(rs), "d_pre_usd_quartiles": quartiles(dpre),
                                  "median_t_rec_low_half": st.median(lo) if lo else None,
                                  "median_t_rec_high_half": st.median(hi) if hi else None,
                                  "median_t_rec_uncensored_only": st.median([r["t_rec"] for r in rs if not r["censored"]]) if any(not r["censored"] for r in rs) else None}

    # ---- chain_degraded / header_gap pool-episodes ----
    out["flagged_episodes"] = {f"{k[0]}#{k[1]}": {"chain_degraded": bool(r.get("chain_degraded")), "header_gap": bool(r.get("header_gap")),
                                                 "dip": r.get("dip"), "local_shock": r.get("local_shock"), "t_rec": r.get("t_rec"),
                                                 "censored": r.get("censored"), "d_pre": r.get("d_pre"), "d_min": r.get("d_min")}
                               for k, r in res.items() if r.get("chain_degraded") or r.get("header_gap")}

    # ---- dedupe counts from the window meta files (8.4) ----
    for chain in CONF_CHAINS + ("unichain",):
        raw = dedup = windows = 0
        for meta in (CONF / "depth" / "windows").glob(f"{chain}_*/ep*/meta.json"):
            m = json.load(open(meta, encoding="utf-8"))
            raw += m.get("logs_raw", 0)
            dedup += m.get("logs_deduped", 0)
            windows += 1
        out["dedupe"][chain] = {"windows": windows, "logs_raw": raw, "logs_deduped": dedup}

    # ---- hashes of run outputs not already in RUNLOG ----
    for name in ["depth/results_ethereum.jsonl", "depth/results_base.jsonl", "depth/results_unichain.jsonl",
                 "depth/results_arbitrum.jsonl", "depth/checks_ethereum.json", "depth/checks_base.json",
                 "depth/checks_unichain.json", "depth/checks_arbitrum.json", "depth/pull_record_ethereum.json",
                 "depth/pull_record_base.json", "depth/pull_record_unichain.json", "depth/pull_record_arbitrum.json",
                 "depth/RUNLOG_depth_ethereum.md", "depth/RUNLOG_depth_base.md", "depth/RUNLOG_depth_unichain.md",
                 "depth/RUNLOG_depth_arbitrum.md", "depth/stage_b_draw.json", "price_only/decision.json",
                 "price_only/price_only_record.json", "analysis/confirmatory_tests.json", "analysis/sensitivities.json",
                 "analysis/ANALYSIS_SUMMARY.md", "logs/analysis_run_record.json"]:
        p = CONF / name
        if p.exists():
            out["hashes"][name] = sha256(p)

    out["labels"] = {
        "pre_registered": ["sd_d_val (7.6)", "per_stratum (7.8 per-chain H1, per-tier H2)", "direction (7.8 up vs down)",
                           "covariate d_pre quartiles (7.8 pre-shock depth covariate)", "fee_switch (7.8 tier contrast pre/post)",
                           "flagged_episodes (7.8 chain_degraded / header_gap)", "dedupe (8.4)", "hashes (8.4 / 10.3 provenance)"],
        "descriptive_not_pre_registered": ["sd_d_rank_nonzero", "covariate median_t_rec_low_half / high_half split",
                                           "covariate median_t_rec_uncensored_only", "d_val_quartiles"],
    }
    OUT_JSON.write_text(json.dumps(out, indent=1), encoding="utf-8")

    # ---- short Markdown summary ----
    L = [f"# Read-only descriptives for the report (generated {out['generated_utc']}; script tools/report_descriptives.py)", "",
         "No test, no p-value, no verdict. Reconstruction of d_e^val per prereg 7.1a/7.4 from results_<chain>.jsonl; reconcile the",
         "`episodes_with_valid_pair`, `n_rank_nonzero` and `median_ratio_reconstructed` columns against confirmatory_tests.json before use.", "",
         "| test | episodes with valid pair | n (rank != 0) | median ratio (reconstructed) | SD of d_e^val (pre-reg 7.6) | SD of d_e^rank, nonzero (descriptive, not pre-registered) |",
         "|---|---|---|---|---|---|"]
    for name, t in out["tests"].items():
        L.append(f"| {name} | {t['episodes_with_valid_pair']} | {t['n_rank_nonzero']} | {t['median_ratio_reconstructed']:.3f} | {t['sd_d_val']:.3f} | {t['sd_d_rank_nonzero']:.3f} |")
    L += ["", "## Per stratum (7.8 descriptive): valid pairs, median ratio, quartiles of r", "", "| test | stratum | pairs | median ratio | q1 r | median r | q3 r |", "|---|---|---|---|---|---|---|"]
    for name, strata in out["per_stratum"].items():
        for s, v in strata.items():
            q = v["quartiles"]
            L.append(f"| {name} | {s} | {v['pairs_valid']} | {v['median_ratio']:.3f} | {q['q1']:.3f} | {q['median']:.3f} | {q['q3']:.3f} |")
    L += ["", "## Up vs down (7.8 descriptive): episodes, median ratio of d_e^val", "", "| test | ETH_down n / median ratio | ETH_up n / median ratio |", "|---|---|---|"]
    for name, d in out["direction"].items():
        dn, up = d.get("ETH_down"), d.get("ETH_up")
        L.append(f"| {name} | {dn['n']} / {dn['median_ratio']:.3f} | {up['n']} / {up['median_ratio']:.3f} |")
    L += ["", "## Tier/chain contrast before and after the fee switch (t0 vs 2025-12-28; 7.8 descriptive)", "", "| test | pre n / median ratio | post n / median ratio |", "|---|---|---|"]
    for name, d in out["fee_switch"].items():
        a, b = d["pre_2025-12-28"], d["post_2025-12-28"]
        L.append(f"| {name} | {a['n']} / {a['median_ratio']:.3f} | {b['n']} / {(b['median_ratio'] if b['median_ratio'] is not None else float('nan')):.3f} |")
    L += ["", "## Pre-shock depth covariate (7.8): D_pre in USD over valid members; T_rec medians by D_pre half (split: descriptive, not pre-registered)", "",
          "| pool | n | D_pre q1 | D_pre median | D_pre q3 | median T_rec, low-D_pre half | median T_rec, high-D_pre half | median T_rec, uncensored only (not pre-reg) |", "|---|---|---|---|---|---|---|---|"]
    for pool, c in out["covariate"].items():
        q = c["d_pre_usd_quartiles"]
        L.append(f"| {pool} | {c['n']} | {q['q1']:,.0f} | {q['median']:,.0f} | {q['q3']:,.0f} | {c['median_t_rec_low_half']} | {c['median_t_rec_high_half']} | {c['median_t_rec_uncensored_only']} |")
    L += ["", "## chain_degraded / header_gap pool-episodes (7.8)", ""]
    for k, v in out["flagged_episodes"].items():
        L.append(f"- {k}: chain_degraded={v['chain_degraded']}, header_gap={v['header_gap']}, dip={v['dip']}, local_shock={v['local_shock']}, T_rec={v['t_rec']}, censored={v['censored']}, D_pre={v['d_pre']:,.0f}, D_min={v['d_min']:,.0f}")
    L += ["", "## Log counts before / after dedupe (8.4), summed over the window meta files", "", "| chain | windows | logs raw | logs deduped |", "|---|---|---|---|"]
    for chain, d in out["dedupe"].items():
        L.append(f"| {chain} | {d['windows']} | {d['logs_raw']:,} | {d['logs_deduped']:,} |")
    L += ["", "## sha256 of run outputs", "", "| file | sha256 |", "|---|---|"]
    for name, h in out["hashes"].items():
        L.append(f"| `{name}` | `{h}` |")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {OUT_JSON}\nwrote {OUT_MD}")
    for name, t in out["tests"].items():
        print(name, "episodes", t["episodes_with_valid_pair"], "n_rank", t["n_rank_nonzero"], "M", round(t["median_ratio_reconstructed"], 4), "sd_val", round(t["sd_d_val"], 4), "sd_rank", round(t["sd_d_rank_nonzero"], 4))
    return 0


if __name__ == "__main__":
    sys.exit(main())
