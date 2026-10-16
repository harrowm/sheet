# sheet

[![tests](https://github.com/harrowm/sheet/actions/workflows/tests.yml/badge.svg)](https://github.com/harrowm/sheet/actions/workflows/tests.yml)
![test count](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/harrowm/sheet/badges/tests.json)

A simple spreadsheet utility. See [specv2.md](specv2.md).

## Development

- `scripts/setup.sh` installs Python 3.14t and dependencies.
- `scripts/test.sh` runs the tests (timing benchmarks are deselected).
- `scripts/bench.sh` runs the parallel-recalculation benchmarks and checks the speedups.
