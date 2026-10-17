"""Transport helpers, the band read against a synthetic archive, and the production drivers end to end on synthetic
chains (price-only phase, depth pull on the R2 route and on the R1+R2 route, the analysis runner). No network."""
import json
import math
import random
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from pipeline import depth_pull, price_only, pull_logs as PL, run_analysis
from pipeline.band_read import band_read
from pipeline.chains import ROUTES, SEED, Live, load_routes
from pipeline.common import BURN_TOPIC, MINT_TOPIC, POOLS, SWAP_TOPIC, block_at_minute_end, timestamp_from_block
from pipeline.depth import depth
from pipeline.reconstruct import state_from_snapshot
from pipeline.transport import (BlockscoutTransport, RateLimiter, RpcError, Stats, get_logs_split, get_logs_with_fallback,
                                is_rate_limited, is_result_cap)
from tests.synth import M0, FakeArchive, FormulaBlocks, SwapPlanner, make_episode, shock_scenario, word

BASE, ETH3, ETH5 = POOLS["base:0.05%"], POOLS["ethereum:0.3%"], POOLS["ethereum:0.05%"]
L = 4_000_000_000_000_000
ALL = [[MINT_TOPIC, BURN_TOPIC, SWAP_TOPIC]]


# ----------------------------------------------------------------------------- transport helpers
def test_result_cap_split_and_markers():
    blocks = {n: (1_700_000_000 + 12 * n, f"0x{n:064x}") for n in range(100)}
    logs = [PL.encode_event(PL.Event(n, 0, "swap", sqrt_price_x96=2 ** 96, liquidity=1, tick=0), BASE.address, blocks[n][1]) for n in range(0, 100, 10)]
    ch = PL.FakeChain(blocks, logs, log_range_cap=1000, result_cap=3)
    tr = PL.FakeTransport(ch)
    before = Stats.splits
    out = get_logs_split(tr, BASE.address, [SWAP_TOPIC], 0, 99)
    assert len(out) == 10 and Stats.splits - before >= 3
    assert is_result_cap("query returned more than 10000 results") and not is_result_cap("rate limit")
    assert is_rate_limited("You reached Public endpoint rate limit") and is_rate_limited("HTTP 429 Too Many Requests")


def test_rate_limiter_paces_without_sleeping_in_tests():
    rl = RateLimiter(10.0)
    rl.wait()
    first = rl._next
    rl.wait()
    assert rl._next - first == pytest.approx(0.1, abs=1e-6)
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_blockscout_transport_shapes_logs(monkeypatch):
    bt = BlockscoutTransport("https://example.invalid/api")
    canned = {"status": "1", "message": "OK", "result": [
        {"address": BASE.address, "topics": [SWAP_TOPIC, "0x" + word(1), "0x" + word(2), None], "data": "0x" + word(0) * 5,
         "blockNumber": "0x10", "timeStamp": "0x65a0", "logIndex": "0x3", "transactionHash": "0xab"},
        {"address": BASE.address, "topics": ["0x" + word(99)], "data": "0x", "blockNumber": "17", "timeStamp": "26000", "logIndex": "4"}]}
    monkeypatch.setattr(bt, "_get", lambda url: canned)
    out = bt.call("eth_getLogs", [{"address": BASE.address, "fromBlock": hex(16), "toBlock": hex(17), "topics": ALL}])
    assert len(out) == 1 and out[0]["blockNumber"] == "0x10" and out[0]["logIndex"] == "0x3" and out[0]["blockTimestamp"] == "0x65a0"
    assert out[0]["topics"] == [SWAP_TOPIC, "0x" + word(1), "0x" + word(2)] and out[0]["blockHash"].startswith("0x")
    out2 = bt.call("eth_getLogs", [{"address": BASE.address, "fromBlock": hex(16), "toBlock": hex(17)}])
    assert len(out2) == 2 and out2[1]["blockNumber"] == hex(17) and out2[1]["logIndex"] == hex(4)
    monkeypatch.setattr(bt, "_get", lambda url: {"status": "0", "message": "No records found", "result": []})
    assert bt.call("eth_getLogs", [{"address": BASE.address, "fromBlock": "0x1", "toBlock": "0x2"}]) == []
    monkeypatch.setattr(bt, "_get", lambda url: {"status": "1", "message": "OK", "result": [canned["result"][0]] * 1000})
    with pytest.raises(RpcError) as ei:
        bt.call("eth_getLogs", [{"address": BASE.address, "fromBlock": "0x1", "toBlock": "0x2"}])
    assert is_result_cap(str(ei.value))
    with pytest.raises(RpcError):
        bt.call("eth_getBlockByNumber", ["0x1", False])


def test_get_logs_with_fallback_switches_source():
    blocks = {n: (1_700_000_000 + 2 * n, f"0x{n:064x}") for n in range(50)}
    logs = [PL.encode_event(PL.Event(5, 0, "swap", sqrt_price_x96=2 ** 96, liquidity=1, tick=0), BASE.address, blocks[5][1])]
    primary = PL.FakeTransport(PL.FakeChain(blocks, logs, serves_logs=False))        # "request limit reached"
    fallback = PL.FakeTransport(PL.FakeChain(blocks, logs))
    before = Stats.fallbacks
    out, src = get_logs_with_fallback(primary, fallback, BASE.address, [SWAP_TOPIC], 0, 49, "t")
    assert src == "fallback" and len(out) == 1 and Stats.fallbacks == before + 1
    with pytest.raises(SystemExit):
        get_logs_with_fallback(primary, None, BASE.address, [SWAP_TOPIC], 0, 49, "t")


def test_routes_override_is_operational(tmp_path):
    f = tmp_path / "routes.json"
    f.write_text(json.dumps({"base": {"logs_url": "https://example.invalid/base", "log_cap": 2000, "paced": False}}))
    routes = load_routes(f)
    assert routes["base"].logs_url == "https://example.invalid/base" and routes["base"].log_cap == 2000 and not routes["base"].paced
    assert routes["base"].calls_url == ROUTES["base"].calls_url and routes["ethereum"] == ROUTES["ethereum"]
    assert load_routes(tmp_path / "missing.json") == ROUTES
    f.write_text(json.dumps({"base": {"no_such_field": 1}}))
    with pytest.raises(ValueError):
        load_routes(f)


# ----------------------------------------------------------------------------- band read against a synthetic archive
def test_band_read_matches_full_state():
    pl = SwapPlanner(BASE, 100, 3000.0, L)
    pl.modify(102, "mint", L // 5, lower=pl.state.tick - 300 - (pl.state.tick - 300) % 10, upper=pl.state.tick + 300 - (pl.state.tick + 300) % 10)
    pl.swap_to(103, 2990.0)
    archive = FakeArchive(BASE, pl.events)
    tr = PL.FakeTransport(PL.FakeChain({n: (0, "0x0") for n in range(110)}, []), archive=archive)
    snap = band_read(tr, BASE, 103, batch=3)
    full = archive.state_at(103)
    assert snap.liquidity == full.liquidity and snap.tick == full.tick and snap.sqrt_price_x96 == full.sqrt_price_x96
    assert snap.liquidity_net == {t: n for t, n in full.liquidity_net.items() if snap.band_lo_tick <= t <= snap.band_hi_tick}
    st = state_from_snapshot(BASE, snap)
    assert depth(st, 0.01).d_z_usd == pytest.approx(depth(full, 0.01).d_z_usd, rel=1e-12)
    with pytest.raises(RpcError):
        band_read(tr, POOLS["base:0.3%"], 103, batch=3)        # no such pool in the archive -> empty slot0


# ----------------------------------------------------------------------------- price-only driver end to end
def _eth_blocks(t0: int, n: int) -> dict:
    return {i: (t0 + 12 * i, "0x" + format(i, "064x")) for i in range(n)}


def _block_at_12s(t0: int):
    return lambda m: max(0, math.ceil((60 * m - t0) / 12))


def _price_only_setup(corrupt: bool = False):
    w_start, w_end = M0, M0 + 3 * 1440
    s_start, s_end = w_start - 120, w_end + 120
    t0 = 60 * (s_start - 60)
    n_blocks = (s_end + 60 - s_start + 60) * 5 + 10
    eth_blocks = _eth_blocks(t0, n_blocks)
    block_at = _block_at_12s(t0)
    shock = M0 + 1440
    pl = SwapPlanner(ETH5, 10, 3000.0, L)
    for m in range(s_start + 5, s_end, 5):
        k = m - shock
        p = 3000.0 if k < 0 else 3000.0 * (1.0 - 0.06 * min(k, 10) / 10)
        pl.swap_to(block_at(m), p)
    logs = []
    for ev in pl.events:
        if ev.kind == "initialize":
            continue
        ts = eth_blocks[ev.block][0] + (1 if corrupt else 0)          # every log-field timestamp off by one second
        logs.append(PL.encode_event(ev, ETH5.address, eth_blocks[ev.block][1], block_timestamp=ts))
    eth = PL.FakeChain(eth_blocks, logs, log_range_cap=10_000)
    shock_ts = 60 * shock

    def prices(chain):
        def ts_of(block):
            return eth_blocks[block][0] if chain in ("ethereum", "arbitrum") else timestamp_from_block(chain, block)

        def archive(to, data, block):
            pool = next((p for p in POOLS.values() if p.address.lower() == to.lower()), None)
            if pool is None or pool.chain == "unichain" or block < 0:
                return "0x"
            p = 3000.0 if ts_of(block) < shock_ts else (2910.0 if pool.key == "base:0.3%" else 2820.0)
            from pipeline.common import sqrt_price_x96_from_usd, tick_at_sqrt_ratio, Q96
            sp = sqrt_price_x96_from_usd(p, pool)
            return "0x" + word(sp) + word(tick_at_sqrt_ratio(sp / Q96)) + word(0) * 5
        return archive

    live = {"ethereum": Live(ROUTES["ethereum"], PL.FakeTransport(eth, archive=prices("ethereum")),
                             PL.FakeTransport(eth, archive=prices("ethereum")), PL.FakeTransport(eth)),
            "arbitrum": Live(ROUTES["arbitrum"], PL.FakeTransport(PL.FakeChain(eth_blocks, [])), PL.FakeTransport(PL.FakeChain(eth_blocks, []), archive=prices("arbitrum")),
                             PL.FakeTransport(PL.FakeChain(eth_blocks, [])))}
    for chain in ("base", "unichain"):
        latest = max(block_at_minute_end(chain, s_end + 100), 1000)
        ch = PL.FakeChain(FormulaBlocks(chain, latest), [], latest_override=latest)
        tr = PL.FakeTransport(ch, archive=prices(chain))
        live[chain] = Live(ROUTES[chain], tr, tr, tr)
    ctx = price_only.Context(Path(), live, s_start, s_end, w_start, w_end, [p for p in POOLS.values() if not p.clock],
                             lo_guess={"ethereum": 0, "arbitrum": 0}, seed_stage_a=False)
    return ctx, shock, eth_blocks


def test_price_only_driver_end_to_end(tmp_path):
    ctx, shock, eth_blocks = _price_only_setup()
    ctx.out = tmp_path / "po"
    rec = price_only.run(ctx)
    assert rec["counts"]["5pct_60min"] == {"N": 1, "N_conf": 0} and rec["counts"]["3pct_30min"]["N"] == 1
    assert rec["decision"]["chosen"] == "3pct_30min"
    eps = json.loads((ctx.out / "episodes_5pct_60min.json").read_text())
    assert len(eps) == 1 and eps[0]["t1_first"] == shock + 10 and eps[0]["direction"] == "ETH_down"   # swaps every 5 min: first observed >= 5% drop
    swaps = json.loads((ctx.out / "swaps" / "summary.json").read_text())
    assert swaps["timestamps"] == "log_field" and swaps["ts_sampled"] > 0 and swaps["ts_mismatches"] == 0
    assert rec["formula_checks"]["base"]["ok"] and rec["formula_checks"]["base"]["sampled"] == 50
    assert rec["formula_checks"]["unichain"]["sampled"] == 0
    dec = json.loads((ctx.out / "decision.json").read_text())
    flags = dec["sets"]["5pct_60min"]["local_flags"][str(eps[0]["episode_id"])]
    assert flags["arbitrum:0.05%"] and flags["ethereum:0.3%"] and not flags["base:0.3%"] and not flags["unichain:0.3%"]
    slot0 = json.loads((ctx.out / "slot0" / "arbitrum_0.05.json").read_text())
    t0, t1 = eps[0]["t0"], eps[0]["t1_first"]
    brute = lambda m: max(n for n, (ts, _) in eth_blocks.items() if ts < 60 * (m + 1))   # noqa: E731
    assert slot0[str(t0)]["block"] == brute(t0) and slot0[str(t1)]["block"] == brute(t1) and slot0[str(t0)]["p"] == pytest.approx(3000.0)
    base_slot0 = json.loads((ctx.out / "slot0" / "base_0.05.json").read_text())
    assert base_slot0[str(t1)]["block"] == block_at_minute_end("base", t1)
    assert set(rec["sha256"]) >= {"series.npz", "episodes_5pct_60min.json", "slot0/arbitrum_0.05.json"}
    assert (ctx.out / "RUNLOG_price_only.md").read_text(encoding="utf-8").startswith("## ")
    # resumable: a second run touches no network for the finished phases
    calls = ctx.live["ethereum"].logs.calls
    price_only.run(ctx)
    assert ctx.live["ethereum"].logs.calls == calls


def test_price_only_stops_on_timestamp_mismatch(tmp_path):
    ctx, _, _ = _price_only_setup(corrupt=True)
    ctx.out = tmp_path / "po_bad"
    with pytest.raises(SystemExit) as ei:
        price_only.run(ctx)
    assert "TIMESTAMP MISMATCH" in str(ei.value)


# ----------------------------------------------------------------------------- depth pull: Base (R2 route)
def _base_setup():
    ep = make_episode(1, M0 + 2000, M0 + 2000)
    latest = block_at_minute_end("base", ep.window_end + 200)
    block_at = lambda m: block_at_minute_end("base", m - 1) + 1          # noqa: E731  first block with ts >= 60m
    init_block = block_at(ep.window_start - 600)
    pl = SwapPlanner(BASE, init_block, 3000.0, L)
    shock_scenario(pl, block_at, ep, 3000.0, L)
    logs = [PL.encode_event(ev, BASE.address, "0x" + format(ev.block, "064x")) for ev in pl.events if ev.kind != "initialize"]
    ch = PL.FakeChain(FormulaBlocks("base", latest), logs, log_range_cap=100, latest_override=latest)
    tr = PL.FakeTransport(ch, archive=FakeArchive(BASE, pl.events))
    live = Live(ROUTES["base"], tr, tr, tr, secondary=PL.FakeTransport(ch))
    return ep, live, init_block, latest, pl


def test_depth_pull_base_r2_route(tmp_path):
    ep, live, init_block, latest, pl = _base_setup()
    ctx = depth_pull.Context(tmp_path / "depth", "base", live, [ep], {1: {"base:0.05%": True, "base:0.3%": True}}, lo_guess=0,
                             creation_override={"base:0.05%": init_block, "base:0.3%": latest + 1})
    rec = depth_pull.run(ctx)
    recs = {r["pool_key"]: r for r in depth_pull.load_records(ctx.out / "results_base.jsonl")}
    r = recs["base:0.05%"]
    assert r["status"] == "ok" and r["dip"] and not r["censored"] and r["t_rec"] == 90 and r["t50"] == 90
    assert r["d_min"] == pytest.approx(0.5 * r["d_pre"], rel=0.05)
    assert r["r2_swaps_unchecked"] == 0 and r["r2_swaps_checked"] > 100 and r["r1"] is None
    assert r["band_reread_count"] >= 1 and {x["reason"] for x in r["rereads"]} <= {"z_edge", "drift", "exit", "dirty"}
    assert r["metrics_by_z"]["0.02"]["t_rec"] == 90 and r["local_shock"] and not r["outage"] and not r["pool_absent"]
    z = np.load(ctx.out / r["series_file"])
    assert len(z["minute"]) == ep.window_end - ep.window_start + 1 and np.all(z["D_0.01"] > 0) and np.all(np.diff(z["block"]) >= 0)
    assert recs["base:0.3%"]["status"] == "pool_absent" and recs["base:0.3%"]["pool_absent"]
    checks = json.loads((ctx.out / "checks_base.json").read_text())
    assert checks["pass"] and checks["formula"]["sampled"] == 50 and checks["completeness"]["base:0.05%"]["compared"] == 50
    assert checks["stage_b_i"]["swaps_unchecked"] == 0 and "D8" in checks["stage_b_ii"]["note"]
    assert rec["checks_pass"] and (ctx.out / "RUNLOG_depth_base.md").exists()
    assert json.loads((ctx.out / "stage_b_draw.json").read_text())["episode_ids"] == [1]


# ----------------------------------------------------------------------------- depth pull: Ethereum (R1 + R2)
def _eth_setup(corrupt: bool = False):
    ep = make_episode(1, M0 + 2000, M0 + 2000)
    t0 = 60 * (ep.window_start - 700)
    n = (ep.window_end - ep.window_start + 1000) * 5 + 10
    blocks = _eth_blocks(t0, n)
    block_at = _block_at_12s(t0)
    init_block = block_at(ep.window_start - 600)
    pl = SwapPlanner(ETH3, init_block, 3000.0, L)
    shock_scenario(pl, block_at, ep, 3000.0, L)
    logs = []
    for ev in pl.events:
        if ev.kind == "initialize":
            continue
        ts = blocks[ev.block][0] + (1 if corrupt else 0)
        logs.append(PL.encode_event(ev, ETH3.address, blocks[ev.block][1], block_timestamp=ts))
    ch = PL.FakeChain(blocks, logs, log_range_cap=10_000)
    tr = PL.FakeTransport(ch, archive=FakeArchive(ETH3, pl.events))
    live = Live(ROUTES["ethereum"], tr, tr, tr, secondary=PL.FakeTransport(ch))
    return ep, live, init_block, n


def test_depth_pull_ethereum_r1_and_r2(tmp_path):
    ep, live, init_block, n = _eth_setup()
    ctx = depth_pull.Context(tmp_path / "depth", "ethereum", live, [ep], {1: {"ethereum:0.3%": True}}, lo_guess=0,
                             creation_override={"ethereum:0.3%": init_block, "ethereum:0.05%": n + 1},
                             lifetime_from={"ethereum:0.3%": init_block})
    depth_pull.run(ctx)
    recs = {r["pool_key"]: r for r in depth_pull.load_records(ctx.out / "results_ethereum.jsonl")}
    r = recs["ethereum:0.3%"]
    assert r["status"] == "ok" and r["t_rec"] == 90 and r["dip"]
    assert r["r1"]["mismatches"] == 0 and r["r1"]["liquidity_check_ok"] and r["r1"]["swaps"] == r["r2_swaps_checked"] + r["r2_swaps_unchecked"]
    assert r["r1_r2"]["pass"] and r["r1_r2"]["max_rel_diff_by_Z"] == [0.0, 0.0, 0.0]
    assert r["ts_check"]["ok"] and r["ts_check"]["sampled"] == min(200, r["log_bearing_blocks"])   # 1%, at least 200 (ruling item 3)
    assert recs["ethereum:0.05%"]["status"] == "pool_absent" and recs["ethereum:0.05%"]["local_shock"]
    checks = json.loads((ctx.out / "checks_ethereum.json").read_text())
    assert checks["pass"] and checks["stage_b_i"]["pass"] and checks["stage_b_ii"]["pass"] and checks["stage_b_ii"]["pool_episodes_compared"] == 1
    assert (ctx.out / "lifetime" / "ethereum_0.3" / "mintburn").exists()


def test_depth_pull_stops_on_timestamp_mismatch(tmp_path):
    ep, live, init_block, n = _eth_setup(corrupt=True)
    ctx = depth_pull.Context(tmp_path / "depth", "ethereum", live, [ep], {1: {"ethereum:0.3%": True}}, lo_guess=0,
                             creation_override={"ethereum:0.3%": init_block, "ethereum:0.05%": n + 1},
                             lifetime_from={"ethereum:0.3%": init_block})
    with pytest.raises(SystemExit) as ei:
        depth_pull.run(ctx)
    assert "TIMESTAMP MISMATCH" in str(ei.value)


def test_draw_five_is_seeded_and_creation_bisection():
    eps = [make_episode(i, M0 + 5000 * i, M0 + 5000 * i) for i in (5, 3, 8, 1, 7, 2, 6, 4)]
    ids = depth_pull.draw_five(eps)
    manual = sorted(e.episode_id for e in random.Random(SEED).sample(sorted(eps, key=lambda e: e.t1_first), 5))
    assert ids == manual == depth_pull.draw_five(list(reversed(eps))) and len(ids) == 5
    tr = PL.FakeTransport(PL.FakeChain({0: (0, "0x0")}, []), code_from_block=1234)
    assert depth_pull.find_creation_block(tr, BASE.address, 0, 5000) == 1234


# ----------------------------------------------------------------------------- analysis runner
def _record(pool_key, eid, t_rec, **over):
    rec = {"pool_key": pool_key, "episode_id": eid, "status": "ok", "d_pre": 100.0, "d_min": 40.0, "dip": True, "t_rec": t_rec,
           "censored": False, "t50": t_rec // 2, "local_shock": True, "outage": False, "chain_degraded": False, "header_gap": False,
           "band_reread_count": 0, "band_read_failed": False, "pool_absent": False,
           "metrics_by_z": {"0.005": {"d_pre": 90.0, "d_min": 30.0, "dip": True, "t_rec": t_rec + 5, "censored": False, "t50": None, "pool_absent": False},
                            "0.02": None}}
    rec.update(over)
    return rec


def test_run_analysis_refuses_failed_checks_and_reports(tmp_path):
    n = 25
    eps = [make_episode(i, M0 + 10000 * i, M0 + 10000 * i) for i in range(1, n + 1)]
    decision = {"decision": {"chosen": "5pct_60min"}, "sets": {"5pct_60min": {"episodes": [asdict(e) for e in eps],
                                                                             "local_flags": {str(e.episode_id): {} for e in eps}}}}
    d = tmp_path / "depth"
    d.mkdir()
    for chain, f in (("ethereum", 1.0), ("arbitrum", 0.8), ("base", 0.8)):
        lines = []
        for e in eps:
            j = 1.0 + 0.01 * e.episode_id
            lines.append(json.dumps(_record(f"{chain}:0.05%", e.episode_id, int(60 * f * j))))
            lines.append(json.dumps(_record(f"{chain}:0.3%", e.episode_id, int(120 * f * j))))
        (d / f"results_{chain}.jsonl").write_text("\n".join(lines) + "\n")
        (d / f"checks_{chain}.json").write_text(json.dumps({"pass": True}))
    out = run_analysis.run(decision, d, tmp_path / "an", ["ethereum", "arbitrum", "base"])
    assert out["tests"]["H1"]["verdict"] == "Yes" and out["tests"]["H1"]["n"] == n
    assert out["tests"]["H2-arbitrum"]["verdict"] in ("Null", "No") and out["tallies"]["H1"]["valid"] == 3 * n
    assert out["resilience_scores"]["ethereum:0.3%"]["n"] == n and (tmp_path / "an" / "ANALYSIS_SUMMARY.md").exists()
    sens = json.loads((tmp_path / "an" / "sensitivities.json").read_text())
    assert "S-h" in sens and "0.005" in sens["S-h"] and "0.02" not in sens["S-h"] and "H2-base" in sens["S-e"]
    (d / "checks_base.json").write_text(json.dumps({"pass": False}))
    with pytest.raises(SystemExit) as ei:
        run_analysis.run(decision, d, tmp_path / "an2", ["ethereum", "arbitrum", "base"])
    assert "REFUSED" in str(ei.value)
