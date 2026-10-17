"""Benchmarks for structural edits (insert/delete rows and columns) and for bulk edits in
sheets full of range formulas.

    uv run python -m benchmarks.structural                 # full run (400,000 cells)
    uv run python -m benchmarks.structural --quick         # small, for a smoke run
    uv run python -m benchmarks.structural --cells 100000 --ranges 5000
    uv run python -m benchmarks.structural --check         # exit 1 if scaling regresses

Scenario 1 builds a sheet of ``--cells`` constants and as many simple formulas (plus
``--ranges`` running-total formulas) and times insert/delete of rows and columns and the
undo and redo of an insert. Scenario 2 rewrites every input of a sheet with a growing number
of range formulas: the time must stay flat as the ranges grow (it used to be proportional to
cells x ranges, 5.7 s for 8,000 ranges against 0.14 s now).

``--check`` fails if scenario 2 gets slower than ``MAX_SCALING`` x from the smallest to the
largest range count, or if an insert changes any value.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from functools import partial
from typing import TYPE_CHECKING

from sheet.engine.sheet import Sheet

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_SCALING = 4.0  # allowed slowdown when the number of range formulas grows 8x (it was 40x)
RANGE_COUNTS = (1_000, 2_000, 4_000, 8_000)


def build(cells: int, ranges: int, workers: int | None = None) -> Sheet:
    """``cells`` constants in A, ``cells`` formulas in B (=A*2), ``ranges`` running sums in C."""
    sheet = Sheet(workers=workers)
    sheet.set_entries([(r, 0, str(r)) for r in range(cells)])
    sheet.set_entries([(r, 1, f"=A{r + 1}*2") for r in range(cells)])
    sheet.set_entries([(r, 2, f"=SUM(A{max(1, r - 8)}:A{r + 1})+(2*3)") for r in range(ranges)])
    sheet.clear_history()
    return sheet


def timed(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def structural(cells: int, ranges: int) -> dict[str, float]:
    """Time the structural operations; also checks an insert changes no value."""
    sheet = build(cells, ranges)
    probes = [(0, 1), (10, 1), (cells - 1, 1)] + ([(ranges - 1, 2)] if ranges else [])
    before = {p: sheet.get(*p) for p in probes}
    results = {
        "insert_rows(0)": timed(lambda: sheet.insert_rows(0)),
    }
    after = {p: sheet.get(p[0] + 1, p[1]) for p in probes}
    if after != before:
        raise AssertionError("an insert changed a value")
    results["undo"] = timed(sheet.undo)
    results["redo"] = timed(sheet.redo)
    results["insert_cols(0)"] = timed(lambda: sheet.insert_cols(0))
    results["delete_cols(0)"] = timed(lambda: sheet.delete_cols(0))
    results["delete_rows(5)"] = timed(lambda: sheet.delete_rows(5))
    sheet.close()
    return results


def bulk_edit_scaling(inputs: int, range_counts: tuple[int, ...]) -> dict[int, float]:
    """Seconds to rewrite every input, for each number of running-total formulas."""
    out: dict[int, float] = {}
    for n_ranges in range_counts:
        sheet = Sheet()
        sheet.set_entries([(r, 0, str(r)) for r in range(inputs)])
        sheet.set_entries([(r, 2, f"=SUM(A{max(1, r - 8)}:A{r + 1})") for r in range(n_ranges)])
        rewrite = [(r, 0, str(r + 1)) for r in range(inputs)]
        out[n_ranges] = timed(partial(sheet.set_entries, rewrite))
        sheet.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--cells", type=int, default=200_000, help="constants (and as many formulas)")
    ap.add_argument(
        "--ranges", type=int, default=0, help="running-total range formulas in scenario 1"
    )
    ap.add_argument("--quick", action="store_true", help="small sizes")
    ap.add_argument("--check", action="store_true", help="exit 1 if scaling regresses")
    args = ap.parse_args(argv)
    cells, ranges, inputs = (5_000, 500, 5_000) if args.quick else (args.cells, args.ranges, 20_000)
    counts = (250, 500, 1_000, 2_000) if args.quick else RANGE_COUNTS

    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(f"Python {sys.version.split()[0]}  GIL enabled: {gil}  CPUs: {os.cpu_count()}")
    print(
        f"\nScenario 1: structural edits on {2 * cells + ranges:,} cells "
        f"({cells:,} constants, {cells:,} formulas, {ranges:,} range formulas)"
    )
    for name, seconds in structural(cells, ranges).items():
        print(f"  {name:18s}{seconds:7.2f} s")

    print(f"\nScenario 2: rewrite {inputs:,} inputs while the sheet holds N running-total formulas")
    times = bulk_edit_scaling(inputs, counts)
    for n, seconds in times.items():
        print(f"  {n:6,d} range formulas {seconds:7.2f} s")
    slowdown = times[counts[-1]] / max(times[counts[0]], 1e-9)
    print(
        f"  {counts[-1] // counts[0]}x more ranges -> {slowdown:.1f}x slower "
        f"(allowed {MAX_SCALING:.0f}x; a scan of every range would be about {counts[-1] // counts[0]}x)"
    )
    if args.check and slowdown > MAX_SCALING:
        print("\nFAILED: bulk edits scale with the number of range formulas again")
        return 1
    if args.check:
        print("\nScaling is flat enough.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
