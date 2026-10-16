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
