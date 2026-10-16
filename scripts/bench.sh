#!/usr/bin/env bash
# Run the parallel-recalculation benchmark table, then the timing assertions.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -m benchmarks.run --check "$@"
uv run pytest -m benchmark -q
