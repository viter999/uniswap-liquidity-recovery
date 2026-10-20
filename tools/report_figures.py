"""Read-only figures for the pilot report (operator ruling 21 Oct 2026: existing outputs only, no re-run).

Figure 1: median and interquartile band of D_Z / D_pre (Z = 1%) versus minutes since t1_last, one panel per
          confirmatory pool (rows = chains, columns = tiers), over the pool-episodes that enter pairs
          (status ok, dip = true, local_shock = true; prereg 7.1a membership conditions).
Figure 2: per-episode d_e^val (log-ratios) for H1, H2-Arbitrum and H2-Base with the MEI line log 1.5, the zero
          line and the median, read from drafts/REPORT_DESCRIPTIVES_20261021.json (which reconciles with
          confirmatory_tests.json).

Inputs: data/confirmatory/price_only/decision.json, data/confirmatory/depth/results_<chain>.jsonl,
        data/confirmatory/depth/series/<pool>/ep<id>.npz (D_0.01, minute), drafts/REPORT_DESCRIPTIVES_20261021.json.
Outputs: drafts/figures/fig1_recovery_curves.svg, drafts/figures/fig2_log_ratios.svg,
         drafts/figures/figures_data.json (the plotted aggregates, input hashes).
The SVG is written with the standard library (no plotting package is installed in the project venv; nothing is
installed). numpy (pinned in requirements.lock) reads the .npz files. Nothing under pipeline/ is imported.

Usage (from the project folder):  .venv/Scripts/python.exe -I tools/report_figures.py
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "data" / "confirmatory"
DESC = ROOT / "drafts" / "REPORT_DESCRIPTIVES_20261021.json"
OUT = ROOT / "drafts" / "figures"

CHAINS = ("ethereum", "arbitrum", "base")
TIERS = ("0.05%", "0.3%")
HORIZON = 4320
STEP = 10  # minutes between plotted points
MEI_LOG = math.log(1.5)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_results() -> dict:
    res = {}
    for chain in CHAINS:
        with open(CONF / "depth" / f"results_{chain}.jsonl", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    res[(r["pool_key"], r["episode_id"])] = r
    return res


def member_ok(r: dict) -> bool:
    return r.get("status") == "ok" and bool(r.get("dip")) and bool(r.get("local_shock")) and not r.get("outage") \
        and not r.get("band_read_failed") and not r.get("pool_absent")


# ---------------------------------------------------------------- figure 1 data
def recovery_curves(decision: dict, res: dict) -> dict:
    t1_last = {e["episode_id"]: e["t1_last"] for e in decision["sets"][decision["decision"]["chosen"]]["episodes"]}
    grid = np.arange(0, HORIZON + 1, STEP)
    out = {}
    for chain in CHAINS:
        for tier in TIERS:
            pool = f"{chain}:{tier}"
            rows = []
            for (pk, ep), r in res.items():
                if pk != pool or not member_ok(r) or not r.get("series_file"):
                    continue
                z = np.load(CONF / "depth" / r["series_file"].replace("\\", "/"))
                rel = z["minute"] - t1_last[ep]
                ratio = z["D_0.01"] / r["d_pre"]
                # state carries forward between samples; take the last sample at or before each grid minute
                idx = np.searchsorted(rel, grid, side="right") - 1
                ok = idx >= 0
                row = np.full(grid.shape, np.nan)
                row[ok] = ratio[idx[ok]]
                rows.append(row)
            m = np.vstack(rows)
            out[pool] = {"n": len(rows), "minutes": grid.tolist(),
                         "q1": np.nanpercentile(m, 25, axis=0).tolist(),
                         "median": np.nanmedian(m, axis=0).tolist(),
                         "q3": np.nanpercentile(m, 75, axis=0).tolist()}
    return out


# ---------------------------------------------------------------- svg helpers
def fmt(x: float) -> str:
    return f"{x:.1f}"


FIG1_YMAX = 2.0  # the largest 75th percentile over all pools is 1.66 (figures_data.json), so nothing is clipped


def svg_fig1(curves: dict, path: Path) -> None:
    """Six panels (rows = chains, columns = tiers). Long captions live in the report, not in the image."""
    left, top, pw, ph, gx, gy = 70, 56, 370, 210, 70, 44
    W = left + 2 * pw + gx + 30
    H = top + 3 * ph + 2 * gy + 70
    ymin, ymax = 0.0, FIG1_YMAX
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<text x="{W/2}" y="24" text-anchor="middle" font-size="14" font-weight="bold">Figure 1. In-range depth after the shock: D_1% / D_pre by minutes since t1_last</text>',
         f'<text x="{W/2}" y="42" text-anchor="middle" fill="#444">line = pointwise median across pool-episodes; band = 25th to 75th percentile; dashed red = the 0.9 recovery level</text>']
    for ci, chain in enumerate(CHAINS):
        for ti, tier in enumerate(TIERS):
            pool = f"{chain}:{tier}"
            c = curves[pool]
            x0, y0 = left + ti * (pw + gx), top + ci * (ph + gy)

            def X(mn): return x0 + pw * mn / HORIZON
            def Y(v): return y0 + ph - ph * (min(max(v, ymin), ymax) - ymin) / (ymax - ymin)
            s.append(f'<rect x="{x0}" y="{y0}" width="{pw}" height="{ph}" fill="none" stroke="#999"/>')
            for v, lab in ((0.5, "0.5"), (0.9, "0.9"), (1.0, "1.0"), (1.5, "1.5"), (2.0, "2.0")):
                dash = ' stroke-dasharray="4 3"' if v == 0.9 else ""
                col = "#c00" if v == 0.9 else "#ddd"
                s.append(f'<line x1="{x0}" x2="{x0+pw}" y1="{fmt(Y(v))}" y2="{fmt(Y(v))}" stroke="{col}"{dash}/>')
                if v == 1.0:  # labelled on the right so it does not touch the 0.9 label on the left
                    s.append(f'<text x="{x0+pw+5}" y="{fmt(Y(v)+4)}" text-anchor="start" fill="#444">1.0</text>')
                else:
                    s.append(f'<text x="{x0-6}" y="{fmt(Y(v)+4)}" text-anchor="end" fill="#444">{lab}</text>')
            for mn in (0, 720, 1440, 2160, 2880, 3600, 4320):
                s.append(f'<line x1="{fmt(X(mn))}" x2="{fmt(X(mn))}" y1="{y0+ph}" y2="{y0+ph+4}" stroke="#666"/>')
                s.append(f'<text x="{fmt(X(mn))}" y="{y0+ph+17}" text-anchor="middle" fill="#444">{mn//60} h</text>')
            pts_up = [f"{fmt(X(m))},{fmt(Y(q))}" for m, q in zip(c["minutes"], c["q3"]) if not math.isnan(q)]
            pts_dn = [f"{fmt(X(m))},{fmt(Y(q))}" for m, q in zip(c["minutes"], c["q1"]) if not math.isnan(q)]
            s.append(f'<polygon points="{" ".join(pts_up + pts_dn[::-1])}" fill="#4a7ebb" fill-opacity="0.22" stroke="none"/>')
            pts_md = [f"{fmt(X(m))},{fmt(Y(q))}" for m, q in zip(c["minutes"], c["median"]) if not math.isnan(q)]
            s.append(f'<polyline points="{" ".join(pts_md)}" fill="none" stroke="#1f4e96" stroke-width="1.8"/>')
            s.append(f'<text x="{x0+6}" y="{y0+15}" font-weight="bold">{chain.capitalize()} {tier}  (n = {c["n"]} pool-episodes)</text>')
    s.append(f'<text x="{W/2}" y="{H-30}" text-anchor="middle" fill="#444">x: minutes since t1_last, shown in hours (0 to 72 h). y: D_1% / D_pre, axis 0 to {FIG1_YMAX:.1f}; nothing is clipped (largest 75th percentile 1.66).</text>')
    s.append(f'<text x="{W/2}" y="{H-12}" text-anchor="middle" fill="#444">Source: depth/series (frozen driver), decision.json, results_&lt;chain&gt;.jsonl; drawn read-only by tools/report_figures.py. Full caption in the report.</text>')
    s.append("</svg>")
    path.write_text("\n".join(s), encoding="utf-8")


FIG2_NAMES = {"H1": "H1", "H2-arbitrum": "H2-Arbitrum", "H2-base": "H2-Base"}
JITTER = 55  # horizontal jitter of the dots around the panel centre (display only, fixed seed)


def svg_fig2(desc: dict, path: Path) -> None:
    """Three strip panels. Dots: filled = in the test with uncensored pairs; hollow = rank-only (every valid pair
    one-censored); grey cross = dropped from the signed-rank test because d_e^rank = 0 (prereg 7.4). The dots at the
    left/right margins are never drawn, so the labels there stay clear of the data."""
    left, top, pw, ph, gx = 60, 60, 260, 330, 48
    W = left + 3 * pw + 2 * gx + 24
    H = top + ph + 112
    ymin, ymax = -8.0, 8.0
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<text x="{W/2}" y="24" text-anchor="middle" font-size="14" font-weight="bold">Figure 2. Per-episode log-ratios d_e^val, with the MEI line (log 1.5 = 0.405) and the median</text>',
         f'<text x="{W/2}" y="42" text-anchor="middle" fill="#444">filled dot = in the test; hollow dot = rank-only (every valid pair one-censored); grey cross = dropped from the test (d_e^rank = 0)</text>']
    rng = np.random.default_rng(1)
    for i, key in enumerate(("H1", "H2-arbitrum", "H2-base")):
        t = desc["tests"][key]
        rows = t["per_episode"]
        vals = [r["d_val"] for r in rows]
        x0 = left + i * (pw + gx)
        cx0 = x0 + pw / 2

        def Y(v): return top + ph - ph * (min(max(v, ymin), ymax) - ymin) / (ymax - ymin)
        s.append(f'<rect x="{x0}" y="{top}" width="{pw}" height="{ph}" fill="none" stroke="#999"/>')
        for v in (-6, -4, -2, 0, 2, 4, 6):
            s.append(f'<line x1="{x0}" x2="{x0+pw}" y1="{fmt(Y(v))}" y2="{fmt(Y(v))}" stroke="{"#888" if v == 0 else "#e6e6e6"}"/>')
            s.append(f'<text x="{x0-6}" y="{fmt(Y(v)+4)}" text-anchor="end" fill="#444">{v}</text>')
        # MEI line, labelled in the left margin above the line (no dot is drawn within JITTER of the panel edges)
        s.append(f'<line x1="{x0}" x2="{x0+pw}" y1="{fmt(Y(MEI_LOG))}" y2="{fmt(Y(MEI_LOG))}" stroke="#c00" stroke-dasharray="5 3" stroke-width="1.5"/>')
        s.append(f'<text x="{x0+4}" y="{fmt(Y(MEI_LOG)-4)}" fill="#c00" font-size="11">MEI 0.405</text>')
        # median of d_e^val over all plotted episodes, labelled in the right margin below the line
        med = float(np.median(vals))
        s.append(f'<line x1="{fmt(cx0-JITTER-12)}" x2="{fmt(cx0+JITTER+12)}" y1="{fmt(Y(med))}" y2="{fmt(Y(med))}" stroke="#1f4e96" stroke-width="2.5"/>')
        s.append(f'<text x="{x0+pw-4}" y="{fmt(Y(med)+13)}" text-anchor="end" fill="#1f4e96" font-size="11">median {med:+.2f}</text>')
        s.append(f'<text x="{x0+pw-4}" y="{fmt(Y(med)+25)}" text-anchor="end" fill="#1f4e96" font-size="11">ratio {math.exp(med):.3f}</text>')
        n_test = n_rank_only = n_dropped = 0
        for r in rows:
            v = r["d_val"]
            cx = cx0 + float(rng.uniform(-JITTER, JITTER))
            tip = f'episode {r["episode_id"]}: d_val = {v:.3f}, valid pairs {r["pairs"]}, uncensored {r["uncensored_pairs"]}, d_rank = {r["d_rank"]:.3f}'
            if r["d_rank"] == 0:
                n_dropped += 1
                s.append(f'<g stroke="#8a8a8a" stroke-width="1.6"><line x1="{fmt(cx-4)}" x2="{fmt(cx+4)}" y1="{fmt(Y(v)-4)}" y2="{fmt(Y(v)+4)}"/><line x1="{fmt(cx-4)}" x2="{fmt(cx+4)}" y1="{fmt(Y(v)+4)}" y2="{fmt(Y(v)-4)}"/><title>{tip} (dropped: d_rank = 0)</title></g>')
            elif r["uncensored_pairs"] == 0:
                n_rank_only += 1
                s.append(f'<circle cx="{fmt(cx)}" cy="{fmt(Y(v))}" r="4" fill="white" stroke="#1f4e96" stroke-width="1.2"><title>{tip} (rank-only)</title></circle>')
            else:
                n_test += 1
                s.append(f'<circle cx="{fmt(cx)}" cy="{fmt(Y(v))}" r="4" fill="#1f4e96" stroke="#1f4e96" stroke-width="1.2"><title>{tip}</title></circle>')
        s.append(f'<text x="{cx0}" y="{top+ph+20}" text-anchor="middle" font-weight="bold">{FIG2_NAMES[key]}</text>')
        s.append(f'<text x="{cx0}" y="{top+ph+36}" text-anchor="middle" fill="#444">{len(vals)} episodes plotted, {n_test + n_rank_only} in the test</text>')
        s.append(f'<text x="{cx0}" y="{top+ph+51}" text-anchor="middle" fill="#444">({n_rank_only} rank-only, {n_dropped} dropped)</text>')
    s.append(f'<text x="{W/2}" y="{H-28}" text-anchor="middle" fill="#444">y: d_e^val (natural log of the recovery-time ratio, hypothesised direction positive); axis -8 to +8, nothing clipped (range -7.18 to 5.91).</text>')
    s.append(f'<text x="{W/2}" y="{H-11}" text-anchor="middle" fill="#444">Source: drafts/REPORT_DESCRIPTIVES_20261021.json (reconciled with confirmatory_tests.json); drawn read-only by tools/report_figures.py. Full caption in the report.</text>')
    s.append("</svg>")
    path.write_text("\n".join(s), encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    decision = json.load(open(CONF / "price_only" / "decision.json", encoding="utf-8"))
    res = load_results()
    desc = json.load(open(DESC, encoding="utf-8"))
    curves = recovery_curves(decision, res)
    svg_fig1(curves, OUT / "fig1_recovery_curves.svg")
    svg_fig2(desc, OUT / "fig2_log_ratios.svg")
    data = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inputs": {"decision.json": sha256(CONF / "price_only" / "decision.json"),
                       "REPORT_DESCRIPTIVES_20261021.json": sha256(DESC),
                       **{f"results_{c}.jsonl": sha256(CONF / "depth" / f"results_{c}.jsonl") for c in CHAINS}},
            "figure1": {pool: {"n": c["n"], "step_min": STEP,
                               "median_at_hours": {str(h): c["median"][h * 60 // STEP] for h in (0, 1, 6, 12, 24, 48, 72)},
                               "q1_at_hours": {str(h): c["q1"][h * 60 // STEP] for h in (0, 1, 6, 12, 24, 48, 72)},
                               "q3_at_hours": {str(h): c["q3"][h * 60 // STEP] for h in (0, 1, 6, 12, 24, 48, 72)}}
                        for pool, c in curves.items()},
            "figure1_full": curves,
            "figure2": {k: {"n_plotted": len(desc["tests"][k]["per_episode"]),
                            "n_in_test": sum(1 for r in desc["tests"][k]["per_episode"] if r["d_rank"] != 0),
                            "rank_only_episodes": [r["episode_id"] for r in desc["tests"][k]["per_episode"] if r["uncensored_pairs"] == 0 and r["d_rank"] != 0],
                            "dropped_episodes_d_rank_zero": [r["episode_id"] for r in desc["tests"][k]["per_episode"] if r["d_rank"] == 0],
                            "median_d_val": float(np.median([r["d_val"] for r in desc["tests"][k]["per_episode"]]))}
                        for k in ("H1", "H2-arbitrum", "H2-base")}}
    (OUT / "figures_data.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    for pool, c in data["figure1"].items():
        print(pool, "n", c["n"], "median at 0/1/6/12/24/48/72 h:", {h: round(v, 3) for h, v in c["median_at_hours"].items()})
    print("figure2", data["figure2"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
