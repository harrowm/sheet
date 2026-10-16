# sheet

[![tests](https://github.com/harrowm/sheet/actions/workflows/tests.yml/badge.svg)](https://github.com/harrowm/sheet/actions/workflows/tests.yml)
![test count](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/harrowm/sheet/badges/tests.json)

A spreadsheet with a parallel recalculation engine. See [specv2.md](specv2.md) for the design.

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
scripts/bench.sh      run the parallel-recalculation benchmarks and check the speedups
```

## Layout

```
sheet/engine/   lexer, parser, evaluator, cell store, dependency graph, parallel recalculation, CSV
sheet/server/   the engine process: request/response protocol and the JSON-lines server
sheet/ui/       PyQt6 client, table model, main window
tests/engine/   engine and protocol tests (3.14t)      tests/ui/   UI tests (3.14 + PyQt6)
benchmarks/     parallel recalculation benchmark shapes and runner
```

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
10. **CSV:** File > Save CSV (values or formulas) and File > Open CSV. Open imports at the
    selected cell.
11. **Calculating indicator:** edit a cell that many formulas depend on. The indicator only
    appears if the engine takes more than about 150 ms.

### Automated tests

```
./scripts/test.sh          # all tests (engine and UI)
./scripts/test.sh ui       # only the UI tests
./scripts/bench.sh         # parallel-recalculation benchmarks
```

### If something goes wrong

- **Window opens but is greyed out, with "Engine error" in red:** the engine process did not
  start. Run `./scripts/setup.sh` again.
- **"The calculation engine could not be started":** same fix as above.
- **Engine messages:** its errors print to the terminal where you ran `run.sh`.
- **Using a different engine interpreter:** set `SHEET_ENGINE_PYTHON=/path/to/python3.14t`
  before `run.sh`.

Known gaps: no cut/move, no drag-fill handle, no native save format (CSV only), and
formatting an entire column is rejected.
