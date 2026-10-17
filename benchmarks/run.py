"""Parallel recalculation benchmarks.

    uv run python -m benchmarks.run               # full table
    uv run python -m benchmarks.run --quick       # small shapes, for a smoke run
    uv run python -m benchmarks.run --check       # exit 1 if parallelism is not working

For every shape the same edit (changing the seed cell) is timed with 1 worker, which is
the sequential baseline, and with several worker counts. The final cell values must be
identical for every worker count.

``--check`` criteria, at ``CHECK_WORKERS`` = min(4, CPUs) workers:
  * wide, lattice, fan_out: speedup of at least ``MIN_EFFICIENCY[shape] * CHECK_WORKERS``;
  * deep (one chain, nothing to parallelise): at most ``MAX_DEEP_SLOWDOWN`` times slower.
Speedups level off once workers outnumber the fast cores (the numbers on a laptop with
4 performance and 6 efficiency cores peak at 4 workers), so the check uses 4, not all CPUs.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from functools import partial
from typing import TYPE_CHECKING

from benchmarks.shapes import SEED_COL, SHAPES, edit_seed, snapshot
from sheet.engine.sheet import Sheet

if TYPE_CHECKING:
    from collections.abc import Callable

CHECK_WORKERS = min(os.cpu_count() or 1, 4)
# Speedup must reach this fraction of CHECK_WORKERS. fan_out has every cell reading one shared
# seed cell, whose reference count all threads contend on (design log entry 85), so its target is
# lower and does not flake when the machine is busy.
MIN_EFFICIENCY = {"wide": 0.5, "lattice": 0.5, "fan_out": 0.4}
MAX_DEEP_SLOWDOWN = 1.3

QUICK = {
    "wide": {"chains": 16, "length": 8},
    "fan_out": {"n": 200},
    "lattice": {"width": 12, "layers": 6},
    "deep": {"length": 100},
}


def default_workers() -> list[int]:
    n = os.cpu_count() or 1
    return sorted({1, 2, 4, n} | ({8} if n >= 8 else set()))


def measure(
    builder: Callable[[Sheet], int], workers: int, repeat: int
) -> tuple[float, float, int, dict]:
    """Return (best seconds per edit, plan seconds, formula cells, final cell values)."""
    with Sheet(workers=workers, parallel_threshold=1) as sheet:
        cells = builder(sheet)
        edit_seed(sheet, 2)  # warm up
        best = float("inf")
        for i in range(repeat):
            start = time.perf_counter()
            edit_seed(sheet, 3 + i)
            best = min(best, time.perf_counter() - start)
        start = time.perf_counter()
        sheet.graph.plan([(0, SEED_COL)])
        return best, time.perf_counter() - start, cells, snapshot(sheet)


def evaluate_results(speedups: dict[str, dict[int, float]]) -> list[str]:
    """Names the targets that were missed; empty if parallelism is working."""
    failures = []
    for name, by_workers in speedups.items():
        s = by_workers.get(CHECK_WORKERS)
        if s is None:
            continue
        if name in MIN_EFFICIENCY and s < MIN_EFFICIENCY[name] * CHECK_WORKERS:
            failures.append(
                f"{name}: speedup {s:.2f}x at {CHECK_WORKERS} workers, "
                f"need {MIN_EFFICIENCY[name] * CHECK_WORKERS:.2f}x"
            )
        if name == "deep" and s < 1 / MAX_DEEP_SLOWDOWN:
            failures.append(f"deep: {1 / s:.2f}x slower with {CHECK_WORKERS} workers")
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--workers", type=lambda s: [int(x) for x in s.split(",")], default=None)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--quick", action="store_true", help="use small shapes")
    ap.add_argument("--check", action="store_true", help="exit 1 if targets are missed")
    args = ap.parse_args(argv)
    workers = args.workers or default_workers()
    if 1 not in workers:
        workers = [1, *workers]

    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(f"Python {sys.version.split()[0]}  GIL enabled: {gil}  CPUs: {os.cpu_count()}")
    if gil:
        print("WARNING: the GIL is enabled, so threads cannot run Python code in parallel.")

    failures: list[str] = []
    speedups: dict[str, dict[int, float]] = {}
    print(
        f"\n{'shape':<9}{'cells':>7}{'plan ms':>9}"
        + "".join(f"{f'{w}w ms':>10}{'x':>7}" for w in workers[1:])
    )
    for name, build in SHAPES.items():
        builder = partial(build, **(QUICK[name] if args.quick else {}))
        base, plan_time, cells, reference = measure(builder, 1, args.repeat)
        row = f"{name:<9}{cells:>7}{plan_time * 1000:>9.1f}"
        speedups[name] = {}
        for w in workers[1:]:
            t, _, _, values = measure(builder, w, args.repeat)
            if values != reference:
                failures.append(f"{name}: results with {w} workers differ from the sequential run")
            speedups[name][w] = base / t
            row += f"{t * 1000:>10.1f}{base / t:>7.2f}"
        print(f"{row}   (sequential: {base * 1000:.1f} ms)")

    if args.check:
        failures += evaluate_results(speedups)
    if failures:
        print("\nFAILED:\n  " + "\n  ".join(failures))
        return 1 if args.check or any("differ" in f for f in failures) else 0
    print(
        "\nResults identical across worker counts."
        + (" Speedup targets met." if args.check else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
