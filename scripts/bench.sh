#!/usr/bin/env bash
# Run the parallel-recalculation benchmarks, the structural-edit and import/export benchmarks, then the timing assertions.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -m benchmarks.run --check "$@"
uv run python -m benchmarks.structural --check
uv run python -m benchmarks.io --check
uv run pytest -m benchmark -q
