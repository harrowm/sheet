"""Timing checks that parallelism works. Slow and machine-dependent, so deselected by
default; run with ``scripts/bench.sh`` (or ``pytest -m benchmark``)."""

import os
import sys
from functools import partial

import pytest

from benchmarks.run import CHECK_WORKERS, evaluate_results, measure
from benchmarks.shapes import SHAPES

pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(sys._is_gil_enabled(), reason="the GIL is enabled"),
    pytest.mark.skipif((os.cpu_count() or 1) < 4, reason="needs at least 4 CPUs"),
]


def test_parallel_recalculation_speeds_up_independent_work_and_never_hurts_chains():
    speedups: dict[str, dict[int, float]] = {}
    for name, build in SHAPES.items():
        base, _, _, reference = measure(partial(build), 1, 3)
        t, _, _, values = measure(partial(build), CHECK_WORKERS, 3)
        assert values == reference
        speedups[name] = {CHECK_WORKERS: base / t}
    assert evaluate_results(speedups) == [], speedups
