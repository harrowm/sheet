#!/usr/bin/env bash
# Set up the development environment: free-threaded Python 3.14t, venv and dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

uv python install 3.14t
uv sync --python 3.14t

# PyQt6 may not yet publish wheels for 3.14t, so the UI extra is installed best-effort.
if uv sync --python 3.14t --extra ui --no-build 2>/dev/null; then
  echo "UI dependencies (PyQt6) installed."
else
  echo "WARNING: PyQt6 could not be installed for 3.14t; the engine works, the UI will not." >&2
fi

uv run python -c "import sys; print('Python', sys.version); print('GIL enabled:', sys._is_gil_enabled())"
echo "Setup complete."
