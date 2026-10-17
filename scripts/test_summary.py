"""Summarise pytest JUnit XML files, as a markdown line or a shields.io endpoint JSON.

    python scripts/test_summary.py a.xml b.xml            # markdown summary line
    python scripts/test_summary.py a.xml b.xml --badge    # shields.io endpoint JSON

Totals are added over all the files (the engine and UI tests are separate runs).
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def read(paths: list[str]) -> dict[str, int]:
    """Totals over one or more JUnit XML files."""
    total = failed = skipped = 0
    for path in paths:
        if not Path(path).exists():  # e.g. an unexpanded glob when no tests ran
            continue
        root = ET.parse(path).getroot()  # noqa: S314 - our own pytest output, not untrusted input
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        total += sum(int(s.get("tests", 0)) for s in suites)
        failed += sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        skipped += sum(int(s.get("skipped", 0)) for s in suites)
    return {
        "total": total,
        "failed": failed,
        "skipped": skipped,
        "passed": total - failed - skipped,
    }


def badge(r: dict[str, int]) -> dict[str, object]:
    ok = r["failed"] == 0 and r["total"] > 0
    message = f"{r['passed']} passed" if ok else f"{r['failed']} failed / {r['total']}"
    return {
        "schemaVersion": 1,
        "label": "tests",
        "message": message,
        "color": "brightgreen" if ok else "red",
    }


def main(argv: list[str]) -> int:
    files = [a for a in argv if not a.startswith("--")]
    if not files:
        print(__doc__)
        return 2
    r = read(files)
    if "--badge" in argv:
        print(json.dumps(badge(r)))
    else:
        print(
            f"**{r['passed']} passed**, {r['failed']} failed, {r['skipped']} skipped "
            f"({r['total']} tests)"
        )
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
