"""The shared sampling convention (6.2), block location (6.6), the Ethereum log-field timestamps with their header
cross-check (D9) and the window sampling helpers."""
import numpy as np
import pytest

from pipeline import pull_logs as PL
from pipeline.blocks import (ArrayIndex, BlockLocator, HeaderCache, bracket, cross_check_timestamps, last_block_before_ts,
                             sample_blocks_in_windows, timestamps_from_logs, verify_formula_on, window_block_range)
from pipeline.common import POOLS, block_at_minute_end, sqrt_price_x96_from_usd, timestamp_from_block
from pipeline.reconstruct import Event
from pipeline.sampling import Clock, WindowSampler, Z_PRIMARY_INDEX
from tests.synth import M0, single_range_state

BASE = POOLS["base:0.05%"]
L = 4_000_000_000_000_000


# ----------------------------------------------------------------------------- 6.2 sampling convention
def test_window_sampler_uses_last_log_bearing_block_before_minute_end():
    """Blocks at known timestamps; the state after each block is tagged by the block number via the liquidity, so the
    sampled minute series must equal the brute-force 'last block with timestamp < 60(m+1)' rule."""
    base_ts = 60 * (M0 + 100)
    blocks = np.array([1000, 1001, 1002, 1003, 1004, 1005], dtype=np.int64)
    ts = np.array([base_ts + 5, base_ts + 59, base_ts + 60, base_ts + 185, base_ts + 240, base_ts + 300], dtype=np.int64)
    clock = Clock(blocks, ts)
    assert clock.minute(1001) == M0 + 100 and clock.minute(1002) == M0 + 101
    assert clock.last_block_in_minute(M0 + 100) == 1001 and clock.last_block_in_minute(M0 + 102) == 1002
    assert clock.last_block_in_minute(M0 + 103) == 1003
    w_start, w_end = M0 + 99, M0 + 106
    sampler = WindowSampler(w_start, w_end, clock, seed_block=999)
    state = single_range_state(BASE, 3000.0, L)
    liq_of_block = {999: L}
    for b in blocks:
        b = int(b)
        sampler.before_block(b, state)
        state.liquidity += 1_000_000_000_000                  # tag the state with the block just applied
        liq_of_block[b] = state.liquidity
    sampler.finish(state)
    assert sampler.complete and sorted(sampler.series) == list(range(w_start, w_end + 1))
    expected_block = {M0 + 99: 999, M0 + 100: 1001, M0 + 101: 1002, M0 + 102: 1002, M0 + 103: 1003, M0 + 104: 1004,
                      M0 + 105: 1005, M0 + 106: 1005}
    for m, rec in sampler.series.items():
        assert rec[5] == expected_block[m], m
    # depth is monotone in the tagged liquidity, so the records of later blocks are larger
    assert sampler.series[M0 + 106][Z_PRIMARY_INDEX] > sampler.series[M0 + 99][Z_PRIMARY_INDEX]
    arrs = sampler.arrays()
    assert list(arrs["minute"]) == list(range(w_start, w_end + 1)) and arrs["block"][1] == 1001 and np.all(arrs["D_0.02"] > 0)
    with pytest.raises(ValueError):
        Clock(np.array([2, 1]), np.array([0, 1]))


# ----------------------------------------------------------------------------- 6.6 block location
def _eth_chain(n_blocks=3000, t0=60 * M0 - 5000):
    blocks = {n: (t0 + 12 * n, f"0x{n:064x}") for n in range(n_blocks)}
    return PL.FakeChain(blocks, []), t0


def test_header_bisection_matches_brute_force_and_caches():
    ch, t0 = _eth_chain()
    tr = PL.FakeTransport(ch)
    cache = HeaderCache()
    loc = BlockLocator("ethereum", "headers", tr, cache, lo_guess=0, hi_guess=ch.latest)
    for m in (M0 + 10, M0 + 11, M0 + 250):
        limit = 60 * (m + 1)
        brute = max(n for n, (ts, _) in ch.blocks.items() if ts < limit)
        assert loc.block_at_minute_end(m) == brute
    calls_before = tr.calls
    loc.block_at_minute_end(M0 + 11)                      # second lookup of a cached minute: no new header reads
    assert tr.calls == calls_before
    assert cache.reads <= 3 * 13 and len(cache) == cache.reads
    b0, b_end = window_block_range(loc, M0 + 20, M0 + 30)
    assert ch.blocks[b0][0] < 60 * (M0 + 20) <= ch.blocks[b0 + 1][0] and ch.blocks[b_end][0] < 60 * (M0 + 31) <= ch.blocks[b_end + 1][0]
    lo, hi = bracket(tr, 60 * (M0 + 5), cache, 0, ch.latest)
    assert ch.blocks[lo][0] < 60 * (M0 + 5) <= ch.blocks[hi][0]
    assert last_block_before_ts(tr, 60 * (M0 + 5), cache, lo, hi) == max(n for n, (ts, _) in ch.blocks.items() if ts < 60 * (M0 + 5))


def test_array_index_tightens_bracket():
    ch, t0 = _eth_chain()
    tr = PL.FakeTransport(ch)
    idx = ArrayIndex([100, 200, 300], [ch.blocks[100][0], ch.blocks[200][0], ch.blocks[300][0]])
    loc = BlockLocator("ethereum", "headers", tr, HeaderCache(), 0, ch.latest, index=idx)
    limit = ch.blocks[250][0] + 1                          # between known blocks 200 and 300
    assert loc.last_block_before(limit) == 250
    assert tr.calls <= 9                                   # log2(100) reads at most, not log2(3000)


def test_formula_locator_and_verification():
    loc = BlockLocator("base", "formula", None, HeaderCache(), 0, 10 ** 9)
    assert loc.block_at_minute_end(M0 + 5) == block_at_minute_end("base", M0 + 5)
    assert timestamp_from_block("base", loc.block_at_minute_end(M0 + 5)) < 60 * (M0 + 6) <= timestamp_from_block("base", loc.block_at_minute_end(M0 + 5) + 1)
    blocks = {n: (timestamp_from_block("base", n), f"0x{n:064x}") for n in range(1000, 1100)}
    tr = PL.FakeTransport(PL.FakeChain(blocks, []))
    picks = sample_blocks_in_windows([(1000, 1049), (1040, 1099)], 20, seed=20261017)
    assert len(picks) == 20 and all(1000 <= b <= 1099 for b in picks) and picks == sample_blocks_in_windows([(1000, 1049), (1040, 1099)], 20)
    assert verify_formula_on(tr, "base", picks, batch=3)["ok"]
    blocks[picks[3]] = (blocks[picks[3]][0] + 2, blocks[picks[3]][1])
    assert verify_formula_on(tr, "base", picks, batch=3)["mismatches"][0][0] == picks[3]


# ----------------------------------------------------------------------------- D9 log-field timestamps
def test_log_timestamps_and_cross_check():
    ch, t0 = _eth_chain(400)
    tr = PL.FakeTransport(ch)
    logs = []
    for n in range(10, 390, 7):
        ev = Event(n, 0, "swap", sqrt_price_x96=sqrt_price_x96_from_usd(3000.0, BASE), liquidity=1, tick=0)
        logs.append(PL.encode_event(ev, BASE.address, ch.blocks[n][1], block_timestamp=ch.blocks[n][0]))
    ts_map = timestamps_from_logs(logs)
    assert ts_map[10] == ch.blocks[10][0] and len(ts_map) == len(logs)
    chk = cross_check_timestamps(tr, ts_map, batch=100, fraction=0.01, minimum=20)
    assert chk["ok"] and chk["sampled"] == 20
    ts_map[17] += 1
    chk2 = cross_check_timestamps(tr, ts_map, batch=100, fraction=1.0, minimum=1)
    assert not chk2["ok"] and chk2["mismatches"][0][0] == 17
    with pytest.raises(PL.RpcError):
        timestamps_from_logs([dict(logs[0], blockTimestamp=None)])
    bad = dict(logs[1])
    bad["logIndex"] = "0x9"
    bad["blockTimestamp"] = hex(ch.blocks[17][0] + 5)
    with pytest.raises(PL.RpcError):
        timestamps_from_logs(logs + [bad])
