"""Uniswap in-range liquidity recovery pilot -- pipeline package (implements frozen prereg v0.3).

Modules
  common            constants, pool table (prereg section 3), price orientation (5.1), tick math, OP-stack timestamps (6.6)
  loadguard         below-normal priority and the free-RAM stop rule
  episodes          trigger/episode detection, consecutive-episode flags, local confirmation, threshold decision (5.2-5.6)
  reconstruct       pool tick-liquidity state: R2 snapshot + window replay, R1 full replay, band re-read (6.1)
  depth             in-range USD depth D_Z and the band-integrated liquidity (6.2)
  pull_logs         RPC log/header pulling with dedupe, cutoff and chunking (8.2, 8.4) -- never run against a real chain here
  price_series      price-only phase: minute series, slot0 reads, N_conf and the primary-threshold decision (5.6)
  resilience_score  per pool-episode metrics (6.3, 6.4) and the test statistics (7.5, 7.9)
  analysis          pairing, episode statistics, verdicts, sensitivities (7.1-7.8)

No module contacts a network unless given a live transport explicitly; the synthetic tests use fakes.
"""
__version__ = "0.3.0-dev"
