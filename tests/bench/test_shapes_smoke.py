"""Fast correctness checks of the benchmark shapes (no timing)."""

import pytest

from benchmarks.run import QUICK, evaluate_results
from benchmarks.shapes import SHAPES, edit_seed, snapshot
from sheet.engine.sheet import Sheet


@pytest.mark.parametrize("name", list(SHAPES))
def test_shapes_give_identical_results_for_any_worker_count(name):
    results = []
    for workers in (1, 3, 6):
        with Sheet(workers=workers, parallel_threshold=1) as sheet:
            SHAPES[name](sheet, **QUICK[name])
            edit_seed(sheet, 5)
            results.append(snapshot(sheet))
    assert results[0] == results[1] == results[2]
    assert len(results[0]) > 50


def test_wide_chains_all_carry_the_seed_value():
    with Sheet(workers=4, parallel_threshold=1) as sheet:
        SHAPES["wide"](sheet, chains=4, length=5)
        edit_seed(sheet, 7)
        assert {sheet.get(j, 4) for j in range(4)} == {7.0}


def test_evaluate_results_applies_the_targets():
    from benchmarks.run import CHECK_WORKERS

    good = {"wide": {CHECK_WORKERS: CHECK_WORKERS * 0.9}, "deep": {CHECK_WORKERS: 0.95}}
    assert evaluate_results(good) == []
    bad = {"wide": {CHECK_WORKERS: 1.0}, "deep": {CHECK_WORKERS: 0.4}}
    if CHECK_WORKERS > 2:  # with 1-2 CPUs a 1.0x speedup is within target
        assert len(evaluate_results(bad)) == 2
