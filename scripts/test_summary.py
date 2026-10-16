"""Print a one-line summary and a shields.io endpoint JSON from a pytest JUnit XML file.

python scripts/test_summary.py test-results.xml            # markdown summary line
python scripts/test_summary.py test-results.xml --badge    # shields.io endpoint JSON
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET


def read(path: str) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
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
    if not argv:
        print(__doc__)
        return 2
    r = read(argv[0])
    if "--badge" in argv:
        print(json.dumps(badge(r)))
    else:
        print(
            f"**{r['passed']} passed**, {r['failed']} failed, {r['skipped']} skipped ({r['total']} tests)"
        )
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
