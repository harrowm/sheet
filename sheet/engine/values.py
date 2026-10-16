"""Cell value types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorKind(Enum):
    DIV0 = "#DIV/0!"
    REF = "#REF!"
    NAME = "#NAME?"
    VALUE = "#VALUE!"
    CIRC = "#CIRC!"
    NUM = "#NUM!"


@dataclass(frozen=True, slots=True)
class ErrorValue:
    kind: ErrorKind
    message: str = ""

    def __str__(self) -> str:
        return self.kind.value


Value = float | str | bool | ErrorValue | None
