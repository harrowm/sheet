# sheet: notes for working on this repository

A spreadsheet: a parallel recalculation **engine** (free-threaded Python 3.14t), a JSON-lines
**server** around it, and a PyQt6 **UI** in a separate process. `README.md` has the user-facing
guide and the benchmark numbers; `specv2.md` is the design, ending in a dated log of every
decision, bug and measurement (entries 1-152). Where the log disagrees with an earlier section,
the later log entry wins. Read the relevant entries before changing an area.

## Two environments, one `pyproject.toml`
- `.venv`: free-threaded 3.14t. Engine, server, tests, benchmarks. PyQt6 does **not** install here.
- `.venv-ui`: standard 3.14 + PyQt6. The UI and its tests only; the UI starts the engine as a
  subprocess (`sheet/ui/launcher.py`) and talks JSON lines over its stdin/stdout.
- `scripts/setup.sh` builds both and enables the pre-commit hook (`core.hooksPath .githooks`).

## Commands
```
scripts/test.sh [engine|ui] [pytest args]   all tests; exits non-zero on failure
scripts/lint.sh [--fast]                    ruff check, ruff format --check, mypy --strict (--fast: ruff only)
scripts/bench.sh                            benchmarks + their checks (timing is machine dependent)
scripts/run.sh                              start the app
```
Before saying work is done: `scripts/lint.sh` and `scripts/test.sh` both clean. CI runs the same
steps on Ubuntu; check the Actions run after pushing (`gh` is not logged in here, so ask the user).

## Layering (enforced by `tests/engine/test_architecture.py`)
`sheet/engine` imports only the engine; `sheet/server` never imports the UI; `sheet/ui` imports
nothing from the engine or server except `sheet.engine.address`.

## Rules of thumb learned the hard way
- **Commit and push only when the user asks.** When they do, add a design-log entry to
  `specv2.md` first (next number after the last), then commit and push. End commits with the
  Co-Authored-By line given in the session's attribution reminder.
- **Never change behaviour without a test first.** Edits are all-or-nothing: anything that
  fails or is cancelled must leave cells, dependency graph and undo history exactly as before.
  The cancel test stops nine kinds of edit at several points and checks this; extend it for new
  edit paths. Long loops call `self._checkpoint()`; pool work goes through
  `ParallelRecalculator.map`, which checks at each chunk.
- **Measure before optimising, with alternating A/B runs** (timings vary about 10% run to run;
  a git worktree of `HEAD` makes a fair baseline). Many "obvious" speed-ups were measured and
  rejected: GC tuning, interning, frozenset building on workers, graph micro-optimisations,
  parallel formatting (all in the log). On free-threaded Python, cheap per-cell tasks run
  *slower* on several threads than on one (reference-count contention on shared objects), so
  find work on one thread and give the pool only cells with real work. Default workers: 4.
- Parser and lexer were rewritten (table-driven) and verified against a saved copy of the old
  one on 600,000 random inputs. Do that kind of differential test for any rewrite.
- A formula's text is regenerated from its *unfolded* parse after reference rewriting (the stored
  AST has constants folded away). Cells cache how far their references reach (`reach_cache`)
  so structural edits skip formulas they cannot affect.
- `Cell` objects are treated as immutable once stored; recalculation replaces them.
- Ruff: default rules plus `ARG C90 N PERF PTH S SIM105 T20 TC` (`pyproject.toml`); `noqa` only
  with a reason. Python 3.14 syntax such as `except A, B:` without parentheses is valid here.
- Do not add features unprompted. The user's standing direction since the hardening review:
  bug-free and bullet-proof over new functionality.

## Where things are
- Request handling and error kinds: `sheet/server/protocol.py`; the loop with the stdin reader
  thread (for `cancel`): `sheet/server/main.py`.
- Edits, undo, structural edits, paste: `sheet/engine/sheet.py` (`_commit` is the one place that
  applies changes, records history and rolls back).
- Reference rewriting for insert/delete/paste: `sheet/engine/refs.py`.
- Test helpers shared by engine tests: `tests/engine/helpers.py`.
