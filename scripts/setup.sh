#!/usr/bin/env bash
# Set up both environments:
#   .venv     free-threaded Python 3.14t: the calculation engine, its tests, the benchmarks
#   .venv-ui  standard Python 3.14 + PyQt6: the user interface
# PyQt6 has no wheel for free-threaded Python, so the UI runs in its own interpreter and
# talks to the engine process over a pipe.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

uv python install 3.14t
uv sync --python 3.14t
UI_PYTHON=$(./scripts/ui_python.sh)
UV_PROJECT_ENVIRONMENT=.venv-ui uv sync --python "$UI_PYTHON" --extra ui

uv run python -c "import sys; print('engine:', sys.version.split()[0], 'free-threaded, GIL enabled:', sys._is_gil_enabled())"
.venv-ui/bin/python -c "import sys; from PyQt6.QtCore import QT_VERSION_STR; print('ui:    ', sys.version.split()[0], 'standard build, Qt', QT_VERSION_STR)"
# Run ruff before every commit (a no-op outside a git checkout).
git rev-parse --git-dir >/dev/null 2>&1 && git config core.hooksPath .githooks
echo "Setup complete."
