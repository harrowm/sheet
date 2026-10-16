#!/usr/bin/env bash
# Start the spreadsheet application.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --extra ui python -m sheet.ui "$@"
