# Spreadsheet Utility – Specification v2

A simple spreadsheet application written in **Python 3.14t** (free-threaded, no GIL), with a PyQt6 UI and a parallel dependency-driven recalculation engine.

## 1. Goals and non-goals

**Goals**
- A correct, well-tested single-sheet spreadsheet with formulas, ranges, formatting, undo/redo and CSV import/export.
- A recalculation engine that uses a dependency DAG and recalculates independent branches in parallel.
- Test-first development, with CI.

**Non-goals (v1)**
- Multiple sheets, named ranges, charts, macros, `.xlsx` import/export.
- Cross-sheet references (`Sheet!A1`).

## 2. Architecture overview

Two layers with a strict dependency direction: **UI -> Engine**. The engine has no Qt imports and is fully testable headless.

```
sheet/
  engine/     lexer, parser, ast, evaluator, cell store, graph, recalc, formats, dates, csv_io, history
  ui/         PyQt6 model, view, main window
  tests/
    engine/   no Qt needed
    ui/       run with QT_QPA_PLATFORM=offscreen
  benchmarks/
  scripts/    setup.sh, run.sh, test.sh
```

Implementation order (each step is complete, tested and working before the next):
1. Lexer, parser, AST, evaluator (single-threaded, correct).
2. Cell store, formats, dates.
3. Dependency graph with cycle detection and rollback.
4. Transactions, undo/redo, structural edits, copy/paste.
5. Parallel recalculation and benchmarks.
6. CSV I/O.
7. PyQt6 UI.

## 3. Project setup and tooling

- Managed with **uv**. `pyproject.toml` pins Python `3.14t` (`uv python install 3.14t`).
- `scripts/setup.sh`: installs the free-threaded Python, creates the venv and syncs dependencies.
- `scripts/run.sh`: starts the spreadsheet application.
- `scripts/test.sh`: runs the full test suite and exits non-zero on any failure.
- Tools: `pytest`, `hypothesis` (property tests for parser, graph and date logic), `pytest-benchmark` or a custom harness, `ruff` (lint and format), `mypy` or `pyright` (type checking).
- **Early spike (before step 1):** confirm that PyQt6 installs and runs on 3.14t and that importing it does not silently re-enable the GIL (check `sys._is_gil_enabled()`). If it does, document the consequence. The engine must still be benchmarked GIL-free in isolation (headless, no Qt import).

## 4. Testing and CI

- **Test first:** for each feature, write failing tests, then implement until they pass.
- Test layers: unit (lexer/parser/evaluator/dates/formats), property-based (hypothesis), integration (engine end-to-end), UI (offscreen Qt), benchmarks.
- **GitHub Actions** workflow runs on every push and pull request: install uv, install 3.14t, run `scripts/test.sh` with `QT_QPA_PLATFORM=offscreen`.
- The README shows a CI status badge (pass/fail). A test count is also shown: CI writes the pytest JUnit summary (tests run / passed / failed) into a badge JSON (for example via a gist and a shields.io endpoint badge), updated on pushes to `main`.

## 5. Grid and cell store

- Addressable grid: **1,048,576 rows x 16,384 columns** (A1 to XFD1048576), as in Excel.
- Storage is a **sparse `dict[(row, col)] -> Cell`**. Only non-empty cells exist. Memory is proportional to the number of populated cells, and reads and writes are O(1).
- There is no CSR structure, no write buffer and no row chunking. The dict is the source of truth. UI responsiveness comes from the threading model in section 10, not from buffering.
- Cell addresses use 0-based `(row, col)` internally; A1 notation is parsed and rendered at the edges only.

### Cell

A `dataclass(slots=True)`:

| Field | Description |
|---|---|
| `text` | The raw text the user entered. |
| `ast` | Root node of the parsed expression, or `None` for non-formula cells. |
| `value` | `float \| str \| bool \| ErrorValue \| None` (None = empty). |
| `format` | Number format string, e.g. `"General"`, `"DD/MM/YYYY"`, `"$#,##0.00"`. Per-cell strings are interned so identical formats share one object. |
| `error` | Error kind and message, when `value` is an `ErrorValue`. |

Rules:
- Entering a number, boolean or text constant creates a cell with no AST; `value` is parsed directly. Entries starting with `=` are formulas.
- **Constant folding:** at parse time any sub-expression made only of literals is replaced with a `Literal` node holding the computed value. Entering `=1/2` stores `text="=1/2"` and an AST that is just `Literal(0.5)`, so evaluation never recomputes it.
- Errors: `#DIV/0!`, `#REF!`, `#NAME?`, `#VALUE!`, `#CIRC!`, `#NUM!`. A formula that references an error cell propagates that error.

## 6. Formula language

- **Parsing:** hand-written lexer and **recursive-descent parser**. Lexing, parsing and AST construction happen **once, on data entry**. Recalculation walks the stored AST and never re-parses.
- **Operators (high to low precedence):** unary `-` `+`; `%` postfix; `^` (right-associative); `*` `/`; `+` `-`; `&` (string concat); comparisons `= <> < > <= >=`. Parentheses are supported.
- **Literals:** numbers (including `1.5e3`), strings in double quotes (`""` escapes a quote), `TRUE` / `FALSE`.
- **References:** `A1`, `$A$1`, `$A1`, `A$1` (case-insensitive); ranges `A1:A45`. Whole-column and whole-row ranges (`A:A`, `1:1`) are supported and resolve to populated cells only.
- **Functions (v1):** `SUM`, `AVERAGE`, `MAX`, `MIN`, `COUNT`. Others may be added through a function registry.
- **Semantics:**
  - Range functions ignore empty cells, text and booleans inside ranges; they error on error values.
  - `AVERAGE` of no numbers returns `#DIV/0!`.
  - Directly referenced empty cells are `0` in arithmetic and `""` in concatenation.
  - Text that cannot be converted to a number in arithmetic gives `#VALUE!`; `TRUE`/`FALSE` coerce to 1/0 in arithmetic.
  - Unknown function names give `#NAME?`.
  - Syntax errors are rejected at entry: the edit is not applied and the user gets a message (the previous cell content is kept).

## 7. Dates and formats

- Dates are stored as a float serial number of days since 1 Jan 1900, **replicating Excel's 1900 leap-year bug**: serial 60 is the nonexistent 29 Feb 1900, so serial 1 = 1 Jan 1900 and serial 61 = 1 Mar 1900. Fractional part = time of day.
- Date recognition: on entry, text matching a supported date pattern (`DD/MM/YYYY`, `YYYY-MM-DD`) is converted to a serial number and the cell format is set to a matching date format.
- **Supported format codes (v1):** `General`; number formats using `0`, `#`, `,`, `.` with optional literal prefix/suffix (`$`, `%`); date tokens `DD`, `MM`, `YYYY`, `YY`, `MMM`, `HH`, `mm`, `SS`. The set is documented and extended through tests.
- Formats can be applied to a single cell or a range. Format changes do not trigger recalculation.
- Format strings are interned in a shared table (see section 5).

## 8. Dependency graph

- Directed acyclic graph with an **adjacency list in both directions**:
  - `dependencies[cell]`: cells this cell reads (used when recalculating: pull values from these).
  - `dependents[cell]`: cells that read this cell (used to find what to recalculate).
- An **in-degree map** is maintained for the affected subgraph during ordering.
- **Kahn's algorithm** performs the topological sort and detects cycles in one pass (nodes left unprocessed indicate a cycle).
- **Localised Kahn:** on a change, collect only the cells reachable downstream via `dependents` from the changed cell(s), then run Kahn on that subgraph only.
- **Range dependencies:** ranges are stored as range nodes, not one edge per cell, so that `A1:A1000000` costs O(1) edges. A change at `(r, c)` finds the range nodes covering it via an index (for example, per-column interval lists). Only populated cells are iterated when a range is evaluated.
- **Atomic edits with rollback:** setting a cell's formula builds the new dependency edges speculatively, runs the cycle check on the affected subgraph, and either commits or restores the previous edges, text, AST and values exactly. A rejected edit shows the user a `#CIRC!` message and leaves the sheet unchanged. Self-reference (`A1 = A1+1`) is covered explicitly.

## 9. Parallel recalculation

- After the graph is ordered, independent nodes are evaluated in parallel by a **thread pool** (`concurrent.futures.ThreadPoolExecutor`, sized to CPU cores) using task-based parallelism.
- Dispatch uses a **pending-dependency counter** per cell (initialised from the in-degree within the subgraph). When a cell finishes, it decrements its dependents' counters, under a lock-protected decrement (Python has no lock-free atomics; the lock must be tiny and per-cell or striped). A cell whose counter reaches zero is submitted to the pool. A global outstanding-task counter signals completion.
- Evaluation of a cell only reads the values of its dependencies (already final) and writes its own value, so no two tasks write the same cell.
- **Sequential fallback:** if the affected subgraph is smaller than a configurable threshold (default to be set from benchmarks), recalculate on the calling thread to avoid overhead.
- Large range functions may additionally be split into parallel partial reductions when benchmarks show a gain.
- **Benchmark suite** (`benchmarks/`): graph shapes (wide fan-out of independent chains, deep chain, diamond lattice, large `SUM` range) with per-cell work that is realistic and also artificially heavy. Reports speedup versus single-threaded at 1, 2, 4, N threads. Success criterion: on the wide independent-chains benchmark with sufficiently heavy cells, speedup at N cores is at least `0.5 * N` (target to be tuned after the first run); deep-chain benchmarks must show no significant slowdown from the pool.

## 10. Threading and edit model

- The Qt GUI thread is the only thread that touches Qt objects. Engine worker threads never call the Qt model; they report completion via Qt signals.
- Edits come from the GUI thread and are placed on an **edit queue**. A single engine coordinator processes edits one at a time and each edit is one transaction: parse, speculative graph update, cycle check, commit or rollback, recalculate.
- **Edits made during a recalculation are queued until it finishes** and are then applied in order. The UI stays responsive because entering text only enqueues work, and the display shows a "calculating" indicator.
- Graph and cell-store mutation is guarded by a **read/write lock** (written for this project, since the standard library has none; writers are preferred to avoid starvation). Recalculation workers take the read side; the coordinator takes the write side for structural changes.
- Readers (the UI model's `data()` calls) read committed values and are never blocked for long: they see the last completed state until the new values are published.

## 11. Transactions, undo and redo

- Every user action (edit, format change, paste, fill, row/column insert/delete) is a single **transaction** recorded as a reversible set of cell changes (text, AST, value, format and graph edges).
- The same mechanism provides cycle rollback (section 8) and **undo/redo**: undo applies the inverse change set and recalculates the affected subgraph; redo reapplies it.
- Undo history depth is configurable (default 100). A new action after an undo clears the redo stack.

## 12. Structural edits, copy/paste and fill

- **Insert/delete rows and columns:** cell keys shift and every formula reference is adjusted (references to deleted cells become `#REF!`; ranges shrink or grow as in Excel). The graph is rebuilt for the affected cells as one transaction. Limits: operations that would push cells beyond the grid bounds are rejected.
- **Copy/paste:** formulas are copied with relative references adjusted by the paste offset (absolute `$` parts are not). Pasting a range is a single transaction. Values-only paste is supported.
- **Fill-down / fill-right:** same adjustment rules as copy/paste.
- Reference adjustment is performed on the AST (not by re-lexing text), then the `text` is regenerated from the AST.

## 13. CSV import/export

- Export writes display values or raw text (selectable); import reads each field through the normal entry path (so formulas and dates are parsed) as one transaction.
- Standard CSV quoting rules (RFC 4180) and UTF-8 encoding.
- Formats are not stored in CSV.

## 14. User interface (PyQt6)

- `QTableView` with a `QAbstractTableModel` subclass over the engine. The model reports the full 1,048,576 x 16,384 size; Qt's view virtualisation means only visible cells are requested.
- Formula bar showing the selected cell's raw `text`; cell editing sends the edit to the engine queue.
- Selection, copy/paste, fill (keyboard and drag handle), undo/redo (Ctrl+Z / Ctrl+Y), a format menu for the selection, row/column insert/delete in the context menu, column header letters and row numbers.
- Open/save CSV through standard dialogs.
- Display text comes from `value` plus `format`; errors display their code (`#DIV/0!`). Numbers are right-aligned, text left-aligned, booleans centred.

## 15. Required test coverage (minimum)

- Lexer and parser: precedence, associativity, unary operators, strings, references, syntax-error cases; property tests that parsing then rendering then parsing is stable.
- Constant folding: `=1/2` yields a `Literal(0.5)` AST and text `"=1/2"`; verify no re-parse occurs during recalculation.
- Evaluator: coercions, empty cells, error propagation, all functions including ranges.
- Dates: serial conversion round trips, including 28 Feb, 29 Feb (bug), and 1 Mar 1900.
- Formats: each supported code with example values.
- Graph: add/remove edges, localized Kahn subgraph extraction, cycle detection (self, direct, indirect), rollback restores the exact prior state; property tests on random DAGs.
- Parallel recalculation: results identical to single-threaded results for random graphs; no cell evaluated before its dependencies; no cell evaluated twice per recalculation.
- Transactions: undo/redo round trips, including formats and structural edits.
- Structural edits and copy/paste: reference adjustment, `#REF!` generation, relative versus absolute.
- CSV: round trips, quoting, formulas and dates on import.
- UI (offscreen): model size and `data()` output, edit round trip, queued edit during recalculation.

## 16. Design decisions and issues found during implementation

A running log of decisions made and issues discovered while building. Newest entries go at the bottom.

### Tooling and platform
1. **PyQt6 has no wheel for free-threaded Python 3.14t.** `uv sync --extra ui` falls back to compiling PyQt6 from source, which hangs or fails. `scripts/setup.sh` installs the `ui` extra wheels-only (`--no-build`) and prints a warning when it is unavailable. The engine is unaffected, but the UI cannot run on 3.14t until a wheel exists. Options for step 7: run the UI on a standard 3.14 build with the engine on 3.14t, or wait for a wheel. Open decision.
2. **The `ui` dependencies are an optional extra**, not a core dependency, so that setup, tests and CI succeed for the engine without Qt.
3. **Layout:** the package is `sheet/` at the repo root and tests are in a top-level `tests/` directory (`tests/engine`, `tests/ui`), rather than nested under the package.

### Lexer and parser
4. **A reference-shaped word followed by `(` is a function name.** `LOG10(` matches the cell-reference pattern (`LOG10` is a valid column and row), so the lexer uses a negative lookahead and treats it as an identifier. Consequence: a function can never be named like a cell reference without parentheses ambiguity, and a reference directly followed by `(` is a syntax error.
5. **Reference bounds are checked in the parser, not the lexer.** `XFE1` and `A1048577` lex as references but are rejected with a syntax error.
6. **Unary minus binds tighter than `^`, as in Excel**: `=-2^2` is 4. `^` is right-associative (`=2^3^2` is 512), which differs from Excel (left-associative, giving 64). The spec and tests chose right-associative; this is a deliberate deviation from Excel and should be revisited if Excel fidelity matters.
7. **Ranges are normalised** so that `start` is the top-left and `end` the bottom-right (`B2:A1` becomes `A1:B2`), with `$` flags following their coordinate.
8. **Function names are upper-cased at parse time;** unknown names are only detected at evaluation (`#NAME?`), not at entry.
9. **Syntax errors are positional:** `FormulaSyntaxError` carries the character position, offset by 1 for the leading `=`.
10. **Numbers are always floats** in the AST (including integer literals) so there is a single numeric type through the engine.
11. **Whole-column and whole-row ranges (`A:A`, `1:1`) are specified (section 6) but not yet implemented in the lexer and parser.** To be added with the range dependency work.

### Evaluator and constant folding
12. **Folding evaluates against an empty reader** (`NO_CELLS`). Any sub-tree with only literals, including function calls such as `SUM(1,2)` and unknown functions, is replaced by a `Literal`. A constant error is folded into an error literal, so `=1/0` is `Literal(#DIV/0!)` and is never re-evaluated.
13. **Folding is purely structural.** Volatile functions (such as `NOW()` or `RAND()`) must not be folded if they are added later; the function registry will need a `volatile` flag.
14. **Comparison ordering** follows Excel: numbers < text < booleans; text compares case-insensitively; an empty cell compares as 0, `""` or FALSE depending on the other operand.
15. **Function argument rules are asymmetric.** A range or single-cell argument ignores empty, text and boolean cells (so `MAX(A1,10)` with text in `A1` ignores `A1`), whereas literal arguments are coerced strictly (`SUM("x")` is `#VALUE!`, `SUM(TRUE)` is 1).
16. **`MAX`/`MIN` of no numbers return 0** and `AVERAGE` of none returns `#DIV/0!`, matching Excel.
17. **Numbers convert to text with 15 significant digits** (`f"{x:.15g}"`), so `5.0` concatenates as `5`.
18. **Arithmetic results that overflow or are not finite give `#NUM!`**; `0^-1` gives `#DIV/0!`; a negative base with a fractional exponent (a complex result) gives `#NUM!`.
19. **`SUM` uses `math.fsum`** for accurate floating-point summation; results may differ in the last digit from a naive left-to-right sum, and from Excel.
20. **A bare range outside a function** (`=A1:A3`) evaluates to `#VALUE!`. Excel's dynamic-array spilling is out of scope.
21. **The evaluator reads cells through a `CellReader` protocol** (`get`, `range_values`) rather than a concrete store, so it can be tested with a fake and the real store plugs in later. `range_values` must return only populated cells, which is what makes huge ranges cheap.
22. **Recursion depth:** parsing and evaluation are recursive, so extremely deeply nested formulas could hit Python's recursion limit. A maximum nesting depth (with a syntax error beyond it) should be added.

### Dates and formats
23. **`make_serial` / `split_serial` return and accept floats**, and the fake `29 Feb 1900` (serial 60) is accepted on entry and displayed, but a real calendar has no such day, so `split_serial(60)` is special-cased. Dates before 1 Jan 1900 are rejected; serial 0 ("0 Jan 1900" in Excel) is not supported.
24. **Times are rounded to the nearest second** before display, and a value such as `45292.999999999` carries into the next day (`02/01/2024 00:00:00`) instead of showing `24:00:00` or `59` seconds.
25. **Date recognition on entry is limited to `D/M/YYYY` and `YYYY-M-D`.** `1/2/2024` is 1 February (day first), consistent with the `DD/MM/YYYY` default; US-style month-first entry is not supported. Ambiguity depends on locale, which is out of scope for v1.
26. **Format tokens are case-sensitive where needed:** `MM`/`MMM` are months and `mm` is minutes, because Excel disambiguates by context and we do not. `DD`, `YY`, `YYYY`, `HH` and `SS` also accept lower case. Single `D`, `M`, `H` etc. (no zero padding) and `MMMM`, `AM/PM` are not supported yet.
27. **A format is numeric or date, never both** (`0 DD` is a `FormatError`). Digit placeholders (`0 # , .`) must be contiguous, so Excel's `0 0` style patterns and multi-section formats (`pos;neg;zero`) are not supported.
28. **Numeric rounding is half away from zero**, done with `Decimal(repr(value))` rather than binary floats, so `2.675` formatted as `0.00` gives `2.68` as Excel does, not `2.67`.
29. **A value that rounds to zero shows no minus sign** (`-0.001` as `0.00` is `0.00`). Excel shows `-0.00`.
30. **Placeholder edge cases deviate from Excel:** `#` with 0 renders an empty string, and `#.##` with `1` renders `1` (Excel shows `1.` with a trailing point).
31. **`%` multiplies the value by 100** wherever it appears in the format. General format shows up to 10 significant digits (`0.3333333333`) and switches to exponent form for very large or small numbers.
32. **Non-numeric values ignore the format** (text, booleans, empty and errors display as General), but an invalid format string raises `FormatError` even then, so bad formats are caught when they are set.
33. **Format strings are compiled once and cached** (`lru_cache`), so rendering a visible grid does not re-parse formats. The cache is thread-safe under free-threaded Python, but the interning of format strings in the cell (section 5) is not yet implemented.

### Entry parsing and the cell store
34. **`Cell.error` is derived, not stored.** The spec listed a separate "has error and message" field, but an `ErrorValue` in `value` already carries the kind and message, so a second field could disagree with it. `Cell.error` is a property returning the `ErrorValue` or `None`.
35. **Entry classification order is: empty, formula (`=`), leading apostrophe, number, boolean, date, text.** A leading `'` forces text (`'123` has value `"123"` and keeps the raw text). Numbers must match a strict pattern, because Python's `float()` would otherwise accept `inf`, `nan`, `1_000` and `infinity` as numbers. Thousands separators (`1,000`) and percent entry (`50%`) are not recognised and are stored as text.
36. **Whitespace around an entry is ignored when classifying** (` 42 ` is the number 42) but the raw text is stored unchanged. A whitespace-only entry is empty.
37. **Constant formulas get their value at entry time.** `=1/2` yields value `0.5` immediately with no recalculation, and `=1/0` yields `#DIV/0!`. Formulas with references have `value=None` until the first recalculation, so a freshly entered formula reads as empty to other cells until then; the coordinator must recalculate it in the same transaction.
38. **A date entry sets the cell's format only if its current format is `General`**, so typing a date into a cell formatted `0.00` keeps `0.00`. Typing a non-date over a date-formatted cell also keeps the date format (as Excel does).
39. **The store replaces `Cell` objects rather than mutating them** (`dataclasses.replace` for format changes), so a reader on another thread sees either the old cell or the new one, never a mix. Format strings are interned (`sys.intern`) when a cell is built.
40. **`range_values` walks the smaller of the rectangle or the stored cells.** Small rectangles are walked by coordinate and huge ones (`A1:XFD1048576`) by scanning the stored cells, so the cost is bounded by the number of populated cells. It skips cells whose value is `None` (empty cells, formatted-only cells and not-yet-calculated formulas). The scan path is O(stored cells) per call, so many large-range formulas over a big sheet will want a per-column or per-row index later; this is the first hot spot to measure.
41. **Formatting a range materialises empty cells**, capped at 1,000,000 cells (`MAX_FORMAT_CELLS`); larger rectangles such as a whole column are rejected with `ValueError`. Formatting entire rows or columns needs a separate style layer (column/row default formats) and is deferred. Applying a format is all-or-nothing.
42. **The store has no locking of its own.** Single dict operations are safe under free-threaded Python, and iteration copies (`list(...)`) to avoid "dict changed size" errors, but multi-step updates are made consistent by the engine's read/write lock (section 10), which does not exist yet. The concurrent-writers test only covers distinct keys.
43. **Bounds are checked on every access** (`IndexError` outside the 1,048,576 x 16,384 grid), including reads.

### Dependency graph
44. **Cycle detection and ordering are one pass.** `DependencyGraph.plan(changed)` walks the downstream subgraph with an iterative search (no recursion, so a 20,000-cell chain is fine), builds the in-degree map, then runs Kahn's algorithm on that subgraph only. Any node left unprocessed means a cycle. The same pass produces the evaluation order, the in-degree map and the successor lists that the parallel recalculation (section 9) needs, so a cycle check and a recalculation plan cost one traversal.
45. **`CycleError.cells` is every unprocessed node, not just the cycle members.** Kahn's algorithm cannot separate cells on the loop from cells merely downstream of it. If the UI should highlight only the loop, a second pass (for example finding strongly connected components) is needed. Open decision.
46. **Edits are atomic by install-check-restore.** `set_dependencies` removes the cell's old edges, installs the new ones, runs `plan([cell])`, and on any exception (not just `CycleError`) uninstalls the new edges and reinstalls the old ones, so the graph is identical to before. A property test compares random edit sequences against a brute-force cycle check and verifies the graph mirrors the model after every step, including after rollbacks.
47. **Only the edited cell needs checking for a new cycle.** The graph is acyclic before the edit, so any new cycle must pass through the edited cell. Kahn run from that cell therefore finds every cycle an edit can create. Self-references (`A1 = A1+1`) and cycles through a range (`C1 = SUM(A1:A3)` with `A2 = C1`) are both covered by tests.
48. **Ranges are single nodes (`Rect`)**, so `A1:A1000000` is one edge set entry. The reverse lookup "which ranges cover this cell" uses a per-column bucket index for ranges up to 64 columns wide, plus a list of wider ranges that is scanned linearly. Within a bucket every range is still checked for row containment, so a column with thousands of ranges costs thousands of comparisons per changed cell. An interval tree per column is the next step if benchmarks show this matters.
49. **`dependents(cell)` allocates and sorts a new set on every call**, and `plan` calls it for every downstream node. That is acceptable now and gives deterministic plans, but it will show up in profiles on very wide fan-outs.
50. **Seeds are part of the plan.** `plan([A1])` includes `A1` itself first, because a changed formula cell must be re-evaluated; a caller that only changed a constant can skip the first element. When several seeds are given and one is downstream of another, the in-degree map counts that edge, so ordering stays correct.
51. **Removing a cell drops only its own reads.** Cells that read the removed cell keep their edges, so they are recalculated (seeing an empty value) when the cell is cleared or overwritten. Deleting a row or column (section 12) will need its own graph rebuild.
52. **The graph is not thread-safe.** All mutation and `plan` calls must hold the engine's write lock (or read lock for `plan`) once the coordinator exists; nothing in the graph enforces this yet.
53. **Dependencies on out-of-bounds or `#REF!` cells are not represented.** The graph stores whatever coordinates it is given; bounds are enforced by the parser and cell store.

### Transactions, undo/redo and the `Sheet` facade
54. **Cells are immutable once stored, so a transaction is a before/after pair of cells per changed position.** Rollback after a rejected edit, undo and redo are all the same operation (`_put_all`): put these cells back. There is no separate "inverse operation" logic to keep in sync.
55. **The dependency graph is derived from the cells' ASTs and is never saved in a transaction.** Putting a cell back re-derives its edges with `extract_references`, using the unchecked `DependencyGraph.restore_dependencies`, because a state that existed before is acyclic. The invariant "graph edges equal the references in each cell's AST" is checked by a randomised test after every operation.
56. **Undo restores values as well as formulas.** Downstream cells recalculated during an edit are recorded in the same transaction, so undo and redo put exact values back and do not recalculate. This makes undo cost proportional to the cells that changed, but it means history holds a copy of every recalculated cell: one edit to a cell that feeds 100,000 formulas stores 100,000 before/after pairs per history entry. Memory use of the 100-step history needs a measurement, and a cap by cell count may be needed.
57. **A batch is checked once.** `set_entries` applies every cell and its edges, then runs a single `plan` from all changed cells. Any new cycle must pass through a changed cell, so one Kahn pass finds it, and the entire batch is rolled back. This makes paste, fill and CSV import O(batch + downstream) rather than one plan per cell. Entries are parsed and bounds-checked before anything is written, so syntax errors and out-of-range positions never reach the rollback path.
58. **Rollback also covers unexpected failures.** Any exception during the write or recalculation phase (not just `CycleError`) restores the touched cells and edges; a test injects a failure partway through recalculation. For a failed or no-op edit there is no history entry.
59. **No-op edits are not recorded.** If, after recalculation, no cell differs from before (for example retyping the same number into a cell), nothing is pushed to the undo stack and the redo stack is kept. "Differs" compares text, AST, format and value, with value comparison type-aware so `TRUE` and `1.0` are different.
60. **A formula that evaluates to an empty cell displays as 0** (`=A1` with `A1` empty gives `0`), as in Excel. Inside larger expressions the empty value is still treated as 0 or `""` depending on the operator.
61. **Clearing a cell keeps a non-General format.** An empty entry removes the cell only if its format is General; otherwise it leaves an empty formatted cell, as Excel's Clear Contents does. Typing into a cell keeps its existing format unless the entry is a date and the format is General (section 5, entry 38).
62. **Every formula in the downstream plan is re-evaluated, including ones whose inputs did not change value.** Pruning ("stop when a recalculated value is unchanged") is a possible later optimisation, but it interacts with the parallel dispatch counters, so it is deferred until after the parallel recalculation exists.
63. **Format changes are transactions without recalculation.** `Sheet.set_format_range` skips the graph and recalculation, since a display format never changes a value. It shares the 1,000,000-cell limit with the cell store (entry 41).
64. **`Sheet` is single-threaded for now.** It holds no locks; the edit queue, read/write lock and worker threads (section 10) are added together with the parallel recalculation. `set_entry` and friends return the set of changed positions so the UI model can emit precise `dataChanged` signals.
65. **Undo depth is bounded by a `deque(maxlen)`** (default 100); the oldest entry is dropped silently. The redo stack is a plain list cleared by any new edit.

### Structural edits, copy/paste and fill
66. **Error literals are part of the formula language.** `#REF!`, `#DIV/0!`, `#NAME?`, `#VALUE!`, `#CIRC!` and `#NUM!` lex as an `ERROR` token and parse to `Literal(ErrorValue(...))`, so a reference that loses its target can be written into the formula text (`=#REF!+1`) and parsed again. An error literal propagates through operators and functions like any other error cell.
67. **Formula text is regenerated from the unfolded parse, not from the stored AST.** Section 12 of the spec said to regenerate text from the AST, but the stored AST has constants folded (`A1+(2*3)` is stored as `A1+6`), so rendering it would silently change what the user typed. Instead a rewritten formula is `parse(text)` (unfolded) -> rewrite references -> `render` -> `compile` again, so the stored AST stays folded. Whitespace and redundant parentheses in a *rewritten* formula are normalised (`=A2+(2*3)` becomes `=A3+2*3`); formulas whose references do not change keep their exact text. A cheap pre-check on the folded AST (folding never removes a reference) skips the parse for unaffected formulas.
68. **`render` uses minimal parentheses and round-trips.** `parse("=" + render(node)) == node` for every tree the parser can produce, verified by a property test over random trees. Precedence levels mirror the parser: `^` is right-associative and a prefix minus binds tighter than `^`, so `-(2^2)` keeps its parentheses and `-2^2` does not. Non-finite number literals (from `1e999`) cannot be rendered and raise `ValueError`.
69. **Insert/delete follow Excel's range rules.** Inserting inside a range or at its start expands or shifts it; inserting just after its last row leaves it alone. Deleting rows shrinks a range, and a range entirely inside the deleted band (or a single reference into it) becomes `#REF!`. Absolute (`$`) references are re-pointed too, since they name cells, not offsets. Only copy, paste and fill treat `$` as fixed.
70. **Structural edits are one transaction, built as a diff.** Every stored cell is mapped to its new position and rewritten; only cells that moved or whose formula changed are put in the change set, plus the positions they vacate. The change set goes through the same commit path as any edit (one recalculation, one cycle check, one undo step, full rollback on failure), and undo restores moved cells, rewritten formulas and values exactly.
71. **Inserting rows or columns that would push a populated cell off the grid is rejected** with `ValueError` and no change; deleting rows or columns past the used area is a no-op that records no history. Argument errors are `ValueError` (count below 1) or `IndexError` (index outside the grid).
72. **Only cells that moved or changed text are recalculated.** A formula whose references are unchanged is not re-evaluated even if it reads cells that moved, which is safe because moves are monotonic: a cell cannot cross a range boundary, and any reference or range that changes shows up as changed text. The randomised test checks values against a fresh rebuild after every operation.
73. **Cost of structural edits is O(populated cells).** Every stored cell is visited to compute its new position. A million-cell sheet therefore pays a full pass per insert or delete; an offset-based row/column index would avoid that but is not needed at the sizes planned for v1.
74. **Paste copies formulas (references shifted), values and formats; empty cells in the copied area clear their targets.** A relative reference shifted off the grid becomes `#REF!`. The clipboard is a snapshot, so copying and pasting over overlapping areas is safe. A paste whose rectangle would leave the grid is rejected. Cut/move is not implemented, since moving a formula without rewriting references to it needs its own semantics.
75. **Paste-values writes constants.** Numbers, booleans and text are written as ordinary entries (text that would otherwise parse as a number, date or formula gets a leading apostrophe), errors are written as `=#DIV/0!` style constant formulas, and target formats are kept. A `values_only` paste of an empty formatted cell clears the target but keeps its format.
76. **Fill copies the first row (down) or first column (right) of the selection** into the remaining cells as a single transaction. Series fill (1, 2, 3 continuing a pattern) and drag-fill extension are not implemented.
77. **`CellStore.cells_in` generalises the range scan.** `range_values`, copy and paste share one routine that walks the cheaper of the rectangle or the stored cells (entry 40), so copying `A1:XFD1048576` costs the number of populated cells.
