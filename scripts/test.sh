#!/usr/bin/env bash
# Run the full test suite; exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen
exec uv run pytest "$@"
