#!/usr/bin/env bash
# Run the tests.
#   scripts/test.sh [pytest args]        engine tests (3.14t) and UI tests (3.14 + PyQt6)
#   scripts/test.sh engine [pytest args] engine tests only
#   scripts/test.sh ui [pytest args]     UI tests only (they start a real engine process)
# Exits non-zero if any test fails. A run that selects no tests (for example because a -k
# filter matches only the other environment) is not a failure.
set -uo pipefail
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen

mode=all
case "${1:-}" in engine | ui) mode=$1; shift ;; esac

status=0
run() { "$@"; local rc=$?; [ $rc -eq 0 ] || [ $rc -eq 5 ] || status=$rc; }

if [ $mode != ui ]; then
  run uv run pytest tests --ignore=tests/ui "$@"
fi
if [ $mode != engine ]; then
  UV_PROJECT_ENVIRONMENT=.venv-ui run uv run --python .venv-ui/bin/python --extra ui pytest tests/ui "$@"
fi
exit $status
