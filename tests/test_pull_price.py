import pytest

from pipeline import pull_logs as PL
from pipeline.common import POOLS, SWAP_TOPIC, sqrt_price_x96_from_usd, tick_at_sqrt_ratio, Q96, timestamp_from_block
from pipeline.price_series import decode_slot0, minute_observations, run_price_only_phase
from pipeline.reconstruct import Event
from tests.synth import M0

POOL = POOLS["base:0.05%"]


def _chain(n_blocks=500, cap=100, genesis=1686789347):
    blocks = {n: (genesis + 2 * n, f"0x{n:064x}") for n in range(n_blocks)}
    logs = []
    for n in (10, 10, 250, 499):
        ev = Event(n, len(logs), "swap", sqrt_price_x96=sqrt_price_x96_from_usd(3000.0 + n, POOL), liquidity=5, tick=0)
        logs.append(PL.encode_event(ev, POOL.address, blocks[n][1]))
    return PL.FakeChain(blocks, logs, log_range_cap=cap)


def test_iter_ranges_respects_cap():
    assert PL.iter_ranges(0, 250, 100) == [(0, 99), (100, 199), (200, 250)]
    assert PL.iter_ranges(5, 5, 1) == [(5, 5)] and PL.iter_ranges(5, 4, 10) == []


def test_get_logs_chunks_and_decodes():
    ch = _chain()
    tr = PL.FakeTransport(ch)
    raw = PL.get_logs(tr, POOL.address, [SWAP_TOPIC], 0, 499, cap=100)
    assert len(raw) == 4 and tr.calls == 5
    with pytest.raises(PL.RpcError):
        PL.get_logs(tr, POOL.address, [SWAP_TOPIC], 0, 499, cap=1000)
    evs = [PL.decode_log(l) for l in PL.dedupe(raw)]
    assert [e.block for e in evs] == [10, 10, 250, 499] and evs[0].liquidity == 5
    assert evs[-1].sqrt_price_x96 == sqrt_price_x96_from_usd(3000.0 + 499, POOL)


def test_dedupe_drops_removed_and_duplicates():
    ch = _chain()
    dup = dict(ch.logs[0])                              # exact duplicate (same blockHash, logIndex) -> dropped
    removed = dict(ch.logs[1]); removed["removed"] = True   # a removed:true copy is discarded; the original stays
    out = PL.dedupe(ch.logs + [dup, removed])
    assert len(out) == 4
    assert [int(l["logIndex"], 16) for l in out] == [0, 1, 2, 3]
    assert len(PL.dedupe([removed])) == 0


def test_headers_batched_and_formula_verification():
    ch = _chain()
    tr = PL.FakeTransport(ch)
    hdr = PL.fetch_headers(tr, range(0, 250), batch_size=100)
    assert len(hdr) == 250 and tr.batches == 3 and hdr[7]["timestamp"] == 1686789347 + 14
    chk = PL.verify_formula(tr, "base", 0, 499, n=50)
    assert chk["ok"] and chk["sampled"] == 50
    ch.blocks[123] = (ch.blocks[123][0] + 1, ch.blocks[123][1])   # corrupt one block
    chk2 = PL.verify_formula(tr, "base", 0, 499, n=500)
    assert not chk2["ok"] and chk2["mismatches"] == [123]
    assert PL.timestamps_for("base", [5, 6]) == {5: timestamp_from_block("base", 5), 6: timestamp_from_block("base", 6)}
    assert PL.header_gaps({1: 0, 2: 700, 4: 710}) == [(1, 2, 700)]


def test_mint_burn_roundtrip_and_cutoff():
    ch = _chain()
    tr = PL.FakeTransport(ch)
    m = Event(7, 3, "mint", tick_lower=-100, tick_upper=200, amount=12345)
    b = Event(8, 0, "burn", tick_lower=-100, tick_upper=200, amount=45)
    for ev in (m, b):
        back = PL.decode_log(PL.encode_event(ev, POOL.address, "0xabc"))
        assert (back.kind, back.tick_lower, back.tick_upper, back.amount) == (ev.kind, ev.tick_lower, ev.tick_upper, ev.amount)
    assert PL.cutoff(tr) == {"block": 499, "hash": f"0x{499:064x}"}


def test_completeness_check_detects_mismatch():
    a, b = _chain(), _chain()
    b.logs = b.logs[:-1]
    out = PL.completeness_check(PL.FakeTransport(a), PL.FakeTransport(b), POOL.address, [10, 250, 499])
    assert not out["ok"] and out["mismatches"] == [(499, 1, 0)]


# ----------------------------------------------------------------------------- price-only phase (5.6)
def test_minute_observations_take_last_swap_in_minute():
    ts = {100: 60 * M0 + 5, 101: 60 * M0 + 30, 102: 60 * M0 + 61}
    swaps = [Event(100, 0, "swap", sqrt_price_x96=sqrt_price_x96_from_usd(3000, POOL)),
             Event(101, 2, "swap", sqrt_price_x96=sqrt_price_x96_from_usd(3010, POOL)),
             Event(101, 1, "swap", sqrt_price_x96=sqrt_price_x96_from_usd(3005, POOL)),
             Event(102, 0, "swap", sqrt_price_x96=sqrt_price_x96_from_usd(2990, POOL))]
    obs = minute_observations(swaps, ts, POOL)
    assert obs[M0] == pytest.approx(3010) and obs[M0 + 1] == pytest.approx(2990)
    word = format(sqrt_price_x96_from_usd(3333.0, POOL), "064x") + "00" * 32
    assert decode_slot0("0x" + word, POOL) == pytest.approx(3333.0, rel=1e-9)


def test_price_only_phase_counts_and_decision():
    clock = POOLS["ethereum:0.05%"]
    non_clock = [p for p in POOLS.values() if p.role == "confirmatory" and not p.clock]
    n = 400 * 24 * 60
    obs = {M0: 3000.0}
    # 30 shocks of -6% at 10-day spacing; local pools: all confirm except base:0.3% on every 3rd shock
    shock_minutes = [M0 + 24 * 60 * 10 * (k + 1) for k in range(30)]
    level = 3000.0
    for m in shock_minutes:
        level *= 0.94
        obs[m] = level
    prices = {}
    for k, m in enumerate(shock_minutes):
        before = obs[shock_minutes[k - 1]] if k else 3000.0
        for p in non_clock:
            local = before * (0.94 if not (p.key == "base:0.3%" and k % 3 == 0) else 0.97)   # 3% move fails 4%
            prices[(p.key, m - 60)] = before
            prices[(p.key, m)] = local

    def price_at(key, minute):
        return prices.get((key, minute), None)

    out = run_price_only_phase(obs, M0, M0 + n, M0, M0 + n, non_clock, price_at)
    prim = out["sets"]["5pct_60min"]
    assert prim["n"] == 30 and prim["n_conf"] == 20 and out["decision"]["chosen"] == "3pct_30min"
    alt = out["sets"]["3pct_30min"]
    assert alt["n"] == 30
