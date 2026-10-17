"""Size limits for formulas, and the interpreter headroom they need.

The limits are Excel's own: 8,192 characters and 64 levels of nesting. Parsing and
evaluating are recursive, so the longest legal formula (about 4,000 terms) needs more
recursion depth than Python's default of 1,000. The recursion limit is raised once, when the
engine is first imported; stack exhaustion is still reported by Python as a RecursionError
(never a crash), which the engine turns into an ordinary error message.
"""

from __future__ import annotations

import sys

MAX_FORMULA_LENGTH = 8192
MAX_NESTING = 64
REQUIRED_RECURSION_LIMIT = 30_000


def ensure_recursion_headroom() -> None:
    if sys.getrecursionlimit() < REQUIRED_RECURSION_LIMIT:
        sys.setrecursionlimit(REQUIRED_RECURSION_LIMIT)
