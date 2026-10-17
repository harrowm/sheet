#!/usr/bin/env bash
# Lint, format-check and type-check everything, in both environments.
#   scripts/lint.sh          ruff check, ruff format --check, mypy --strict (engine, server, UI)
#   scripts/lint.sh --fast   ruff only (what the pre-commit hook runs)
# Runs every step even if an earlier one fails, then exits non-zero if any did.
set -uo pipefail
cd "$(dirname "$0")/.."

dirs="sheet tests benchmarks scripts"
status=0
step() { echo "== $*"; "$@" || status=1; }

step uv run ruff check $dirs
step uv run ruff format --check $dirs
if [ "${1:-}" != "--fast" ]; then
  step uv run mypy sheet --exclude 'sheet/ui'
  # The UI needs PyQt6's stubs, which live in the standard-Python environment.
  step env UV_PROJECT_ENVIRONMENT=.venv-ui uv run --python .venv-ui/bin/python --extra ui mypy sheet/ui
fi
[ $status -eq 0 ] && echo "Lint clean." || echo "Lint FAILED." >&2
exit $status
