"""Cell value types."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


class ErrorKind(Enum):
    DIV0 = "#DIV/0!"
    REF = "#REF!"
    NAME = "#NAME?"
    VALUE = "#VALUE!"
    NUM = "#NUM!"


@dataclass(frozen=True, slots=True)
class ErrorValue:
    kind: ErrorKind
    message: str = ""

    def __str__(self) -> str:
        return self.kind.value


Value = float | str | bool | ErrorValue | None

# The one definition of a decimal number, used by the formula lexer and by the text-to-number
# rules for typed entries and for arithmetic on text. ASCII digits only.
NUMBER_PATTERN = r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_SIGNED_NUMBER = re.compile(r"[+-]?" + NUMBER_PATTERN, re.ASCII)


def parse_number(text: str) -> float | None:
    """The finite number ``text`` spells exactly (optional sign, no spaces), else None.

    A number beyond the float range counts as not a number, so it stays text.
    """
    if _SIGNED_NUMBER.fullmatch(text) and math.isfinite(number := float(text)):
        return number
    return None
