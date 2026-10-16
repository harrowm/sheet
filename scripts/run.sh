#!/usr/bin/env bash
# Start the spreadsheet (the UI starts the engine process itself).
set -euo pipefail
cd "$(dirname "$0")/.."
UV_PROJECT_ENVIRONMENT=.venv-ui exec uv run --python .venv-ui/bin/python --extra ui python -m sheet.ui "$@"
