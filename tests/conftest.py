"""Test session guard: the operator's load rule applies to test runs too (below-normal priority; stop if free RAM < 3 GB)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import loadguard  # noqa: E402
from pipeline.transport import Stats  # noqa: E402

Stats.enabled = False   # no RAM gate and no retry sleeps inside the synthetic suite (the drivers run gated)

_free = loadguard.apply("tests")  # raises SystemExit (LoadRuleStop) below the threshold; pytest then stops
