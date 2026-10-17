# sheet

[![tests](https://github.com/harrowm/sheet/actions/workflows/tests.yml/badge.svg)](https://github.com/harrowm/sheet/actions/workflows/tests.yml)
![test count](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/harrowm/sheet/badges/tests.json)

A spreadsheet with a parallel recalculation engine. Supported platforms: macOS and Linux (Windows is untested). See [specv2.md](specv2.md) for the design.

- **Engine** (free-threaded Python 3.14t): formulas, ranges, dates and formats, dependency graph
  with cycle rollback, undo/redo, row/column insert and delete, copy/paste/fill, CSV, and parallel
  recalculation across all cores.
- **UI** (standard Python 3.14 + PyQt6): a `QTableView` over the full 1,048,576 x 16,384 grid, formula
  bar, name box, menus and a status bar. PyQt6 has no free-threaded wheel, so the UI runs in its own
  interpreter and starts the engine as a subprocess, talking to it with JSON lines over a pipe.

```
scripts/setup.sh      install both environments (.venv for the engine, .venv-ui for the UI)
scripts/run.sh        start the spreadsheet
scripts/test.sh       run all tests  (test.sh engine | test.sh ui for one half)
scripts/bench.sh      run the benchmarks (recalculation, structural edits, import/export) and check them
scripts/lint.sh       ruff, format check and mypy --strict (lint.sh --fast: ruff only)
```

## Layout

```
sheet/engine/   lexer, parser, evaluator, cell store, dependency graph, parallel recalculation, CSV
sheet/server/   the engine process: request/response protocol and the JSON-lines server
sheet/ui/       PyQt6 client, table model, main window
tests/engine/   engine and protocol tests (3.14t)      tests/ui/   UI tests (3.14 + PyQt6)
benchmarks/     recalculation shapes and runner, structural-edit, and import/export/paste benchmarks
scripts/        setup, run, test, bench and lint scripts      .githooks/  pre-commit hook (ruff)
```

## Performance

Measured with `./scripts/bench.sh` on an Apple M4 (4 performance + 6 efficiency cores), free-threaded
Python 3.14.8. Timings vary by about 10% from run to run; the checks in `bench.sh` fail only if a
property such as "more workers never hurts a chain" or "bulk edits do not scale with the number of
ranges" stops holding, not on absolute times.

**Parallel recalculation** (`benchmarks.run`): the same graph evaluated with 1 and with N workers.

| Shape | Cells | Sequential | 4 workers | Speedup |
|---|---|---|---|---|
| wide (independent chains) | 1,280 | 100 ms | 35 ms | 2.9x |
| fan-out | 1,500 | 118 ms | 42 ms | 2.8x |
| diamond lattice | 640 | 50 ms | 17 ms | 2.9x |
| deep chain (nothing to parallelise) | 600 | 47 ms | 48 ms | 1.0x |

The default is `min(CPUs, 4)` workers (`SHEET_WORKERS` overrides it): more threads were slower on
every shape, because threads that share objects contend on their reference counts.

**Row and column inserts and deletes** (`benchmarks.structural`): a sheet that is half constants
and half formulas, edited at the top (the worst case: every cell moves and every formula is rewritten).

| Operation | 400,000 cells | 800,000 cells |
|---|---|---|
| insert rows | 1.7 s | 3.5 s |
| insert columns | 2.0 s | 4.5 s |
| delete columns (breaks every formula) | 2.2 s | 4.5 s |
| delete rows | 1.9 s | 4.0 s |
| undo / redo of an insert | 0.35 s | 0.8 s |

Edits further down touch far less: on 800,000 cells, deleting a row near the bottom takes 0.1 s,
inserting a column to the right of everything 0.09 s, a row in the middle 2.0 s. With 50,000 range
formulas on a 450,000-cell sheet an insert at the top takes 4.4 s. Rewriting 20,000 inputs in a
sheet full of range formulas stays flat (0.09 s with 1,000 ranges, 0.14 s with 8,000).

**Files, CSV and paste** (`benchmarks.io`): 100,000 rows, 210,000 cells.

| Operation | Time | Cells/s |
|---|---|---|
| save `.sheet` | 0.28 s | 745k |
| load `.sheet` | 1.6 s | 134k |
| export CSV (as typed / as displayed) | 0.09 s / 0.12 s | 2,400k / 1,700k |
| import CSV | 1.8 s | 119k |
| paste text | 1.1 s | 192k |
| copy / paste 100,000 rows of formulas | 0.05 s / 1.5 s | 4,600k / 143k |

Loading, importing and pasting are dominated by recalculating every formula, which the file format
does not store (a file can never disagree with its formulas). **Interactive edits** on a 400,000-cell
sheet take under 0.1 ms (undo, redo, copy, typing a value that feeds formulas).

Every operation can be stopped: a request that runs longer than 60 seconds (`SHEET_TIME_LIMIT`
changes it, 0 turns it off) is abandoned, and the UI shows a Cancel button while the engine is
busy. A stopped edit leaves the document exactly as it was.

## How to test

### Run the app

```
./scripts/setup.sh     # once
./scripts/run.sh       # opens the window
```

The window starts the calculation engine itself, so there is nothing else to launch. The
bottom-right of the status bar should read "Engine: free-threaded Python (GIL off)".

### Things to try

1. **Typing and formulas**
   - Click a cell and type `5`, then Enter. The cursor moves down.
   - In `B1`, type `=A1*2`. Change `A1` and watch `B1` update.
   - Try `=SUM(A1:A10)`, `=AVERAGE(...)`, `=MAX(...)` and `=MIN(...)`.
2. **Editing**
   - F2 or double-click edits a cell in place.
   - The formula bar above the grid shows the raw text. Enter commits and Escape cancels.
3. **Navigation**
   - Type `XFD1048576` in the box at the top left to jump to the far corner.
   - Scrolling should stay smooth across the whole 1,048,576 x 16,384 grid.
4. **Errors**
   - `=1/0` shows a red `#DIV/0!`.
   - `=1+` is rejected with a red message in the status bar.
   - Set `A1` to `=B1`, then `B1` to `=A1`. The second edit is refused as a circular
     reference and nothing changes.
5. **Formats and dates**
   - Select cells, then use the **Format** menu (Currency, Percent, Date and so on).
   - Typing `25/12/2024` becomes a date automatically.
6. **Undo and redo:** Ctrl+Z, and Ctrl+Shift+Z or Ctrl+Y. Undo restores formulas and
   dependent values together.
7. **Copy and paste**
   - Copy `B1` (`=A1*2`) and paste into `B3`. It becomes `=A3*2`.
   - Ctrl+Shift+V pastes values only.
   - Paste tab-separated text from elsewhere, for example from another spreadsheet.
8. **Clear and fill**
   - Delete clears the selection.
   - Select a column range and press Ctrl+D to fill down, or Ctrl+R to fill right.
9. **Rows and columns:** right-click a row or column header to insert or delete.
10. **Save and open:**
    - Ctrl+S saves. The first time it asks for a name and adds `.sheet`.
    - Ctrl+Shift+S saves under a new name, Ctrl+O opens a file, and Ctrl+N starts a new sheet.
    - The title shows the file name, with a `*` when there are unsaved changes.
    - New, Open and closing the window ask before throwing away unsaved changes.
    - A saved file keeps formulas and formats. Reopening it recalculates everything and
      starts with empty undo history.
    - Open a hand-damaged file (for example, edit a `.sheet` file in a text editor and break
      the JSON). You should get a red message, and your current sheet must stay untouched.
11. **CSV:** File > Import CSV and File > Export CSV (values or formulas). Import fills in
    from the selected cell. CSV is for exchanging data; it does not become the document
    you save.
12. **Calculating indicator:** edit a cell that many formulas depend on. The indicator only
    appears if the engine takes more than about 150 ms.

### Automated tests

```
./scripts/test.sh          # all tests (engine and UI)
./scripts/test.sh ui       # only the UI tests
./scripts/bench.sh         # benchmarks
./scripts/lint.sh          # ruff + mypy; setup.sh also installs a pre-commit hook running lint.sh --fast
```

### If something goes wrong

- **Window opens but is greyed out, with "Engine error" in red:** the engine process did not
  start. Run `./scripts/setup.sh` again.
- **"The calculation engine could not be started":** same fix as above.
- **Engine messages:** its errors print to the terminal where you ran `run.sh`.
- **Using a different engine interpreter:** set `SHEET_ENGINE_PYTHON=/path/to/python3.14t`
  before `run.sh`.

Known gaps: no cut/move, no drag-fill handle, column widths are not saved, and formatting an
entire column is rejected.
