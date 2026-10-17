"""Benchmarks for getting data into and out of a sheet: native save/load, CSV and paste.

    uv run python -m benchmarks.io                  # full run (100,000 rows, 200,000+ cells)
    uv run python -m benchmarks.io --quick          # small, for a smoke run
    uv run python -m benchmarks.io --rows 50000
    uv run python -m benchmarks.io --check          # exit 1 if a round trip loses anything

The sheet has ``--rows`` rows of a constant (column A), a formula (``=A1*2``, column B) and, on
every tenth row, a small range formula (column C). Each operation is timed on its own and the
result is checked against the original: a load, a CSV import or a paste that changes a value
is a failure, however fast it was. Paste and typed-in text are limited to 250,000 cells at a
time (``MAX_WRITE_CELLS``), so the paste scenarios use at most that many.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sheet.engine.csv_io import export_csv, import_csv
from sheet.engine.files import load_sheet, save_sheet
from sheet.engine.limits import MAX_WRITE_CELLS
from sheet.engine.sheet import Sheet

if TYPE_CHECKING:
    from collections.abc import Callable

    from sheet.engine.values import Value


def entries(rows: int) -> list[tuple[int, int, str]]:
    """The benchmark sheet's contents: constants, simple formulas and some range formulas."""
    out: list[tuple[int, int, str]] = []
    for r in range(rows):
        out.append((r, 0, str(r + 1)))
        out.append((r, 1, f"=A{r + 1}*2"))
        if r % 10 == 0:
            out.append((r, 2, f"=SUM(A{max(1, r - 8)}:A{r + 1})+B{r + 1}"))
    return out


def build(rows: int) -> Sheet:
    sheet = Sheet()
    sheet.set_entries(entries(rows))
    sheet.clear_history()
    return sheet


def snapshot(sheet: Sheet) -> dict[tuple[int, int], tuple[str, Value]]:
    return {pos: (cell.text, cell.value) for pos, cell in sheet.store.items()}


def timed[T](fn: Callable[[], T]) -> tuple[float, T]:
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def run(rows: int) -> tuple[dict[str, float], list[str]]:
    """Time each operation. Returns (seconds by name, descriptions of any wrong results)."""
    times: dict[str, float] = {}
    problems: list[str] = []
    source = build(rows)
    expected = snapshot(source)
    cells = len(expected)

    def verify(name: str, sheet: Sheet) -> None:
        if snapshot(sheet) != expected:
            problems.append(f"{name}: the result differs from the original")

    with tempfile.TemporaryDirectory() as tmp:
        native = Path(tmp) / "bench.sheet"
        times["save (.sheet)"], _ = timed(lambda: save_sheet(source, native))
        times["load (.sheet)"], loaded = timed(lambda: load_sheet(native))
        verify("load", loaded)
        loaded.close()

        raw = Path(tmp) / "raw.csv"
        times["export CSV (raw)"], _ = timed(lambda: export_csv(source, raw, mode="raw"))
        target = Sheet()
        times["import CSV (raw)"], _ = timed(lambda: import_csv(target, raw))
        verify("import CSV", target)
        target.close()

        shown = Path(tmp) / "display.csv"
        times["export CSV (display)"], _ = timed(lambda: export_csv(source, shown))

    # Paste: typed-in text is split on tabs and newlines and entered as one transaction.
    paste_rows = min(rows, MAX_WRITE_CELLS // 3)
    text = "\n".join("\t".join(row) for row in _text_rows(paste_rows))
    lines = [line.split("\t") for line in text.split("\n")]
    pasted = Sheet()
    times["paste text"], _ = timed(
        lambda: pasted.set_entries(
            (r, c, field) for r, line in enumerate(lines) for c, field in enumerate(line)
        )
    )
    if snapshot(pasted) != {p: v for p, v in expected.items() if p[0] < paste_rows}:
        problems.append("paste text: the result differs from the original")
    pasted.close()

    # Copy a block and paste it elsewhere: relative references shift and are re-entered.
    block_rows = min(rows, MAX_WRITE_CELLS // 3)
    clip = source.copy(0, 0, block_rows - 1, 2)
    times["copy block"], _ = timed(lambda: source.copy(0, 0, block_rows - 1, 2))
    times["paste block"], _ = timed(lambda: source.paste(clip, 0, 4))
    if source.get(0, 5) != source.get(0, 1) or source.get(block_rows - 1, 5) != source.get(
        block_rows - 1, 1
    ):
        problems.append("paste block: a pasted formula has the wrong value")
    source.close()
    times["_cells"] = float(cells)
    return times, problems


def _text_rows(rows: int) -> list[list[str]]:
    out: list[list[str]] = []
    for r in range(rows):
        row = [str(r + 1), f"=A{r + 1}*2"]
        if r % 10 == 0:
            row.append(f"=SUM(A{max(1, r - 8)}:A{r + 1})+B{r + 1}")
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--quick", action="store_true", help="small sizes")
    ap.add_argument("--check", action="store_true", help="exit 1 if any result is wrong")
    args = ap.parse_args(argv)
    rows = 2_000 if args.quick else args.rows

    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(f"Python {sys.version.split()[0]}  GIL enabled: {gil}  CPUs: {os.cpu_count()}")
    times, problems = run(rows)
    cells = int(times.pop("_cells"))
    print(f"\nImport, export and paste on {cells:,} cells ({rows:,} rows)")
    for name, seconds in times.items():
        print(f"  {name:22s}{seconds:7.2f} s   {cells / max(seconds, 1e-9) / 1000:9,.0f}k cells/s")
    if problems:
        print()
        for problem in problems:
            print(f"WRONG: {problem}")
        return 1 if args.check else 0
    if args.check:
        print("\nEvery round trip reproduced the original.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
