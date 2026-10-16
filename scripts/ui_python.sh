#!/usr/bin/env bash
# Print the path of a standard (GIL) CPython 3.14, installing one with uv if none exists.
# The UI needs it because PyQt6 has no free-threaded wheel. uv's "3.14+gil" request is not
# used: it finds nothing on a fresh machine, so pick by the "variant" uv reports instead.
set -euo pipefail

find_python() {
  uv python list --only-installed --output-format json | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if e["implementation"] == "cpython" and e["variant"] == "default" and e["version"].startswith("3.14."):
        print(e["path"])
        break
'
}

path=$(find_python)
if [ -z "$path" ]; then
  uv python install 3.14 >&2
  path=$(find_python)
fi
[ -n "$path" ] || { echo "could not find or install a standard Python 3.14" >&2; exit 1; }
echo "$path"
