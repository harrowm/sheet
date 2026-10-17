"""Display formats: ``General``, numeric (``#,##0.00``, ``$0.0%``) and date/time (``DD/MM/YYYY``).

Date tokens are case-sensitive where Excel would need context to tell them apart:
``MM``/``MMM`` are months, ``mm`` is minutes. ``DD``, ``YY``, ``YYYY``, ``HH`` and ``SS``
also accept lower case. A format is either numeric or date, never both. Text in double
quotes is copied literally.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import lru_cache

from sheet.engine.dates import split_serial
from sheet.engine.values import ErrorValue, Value

GENERAL = "General"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DATE_TOKENS = ("YYYY", "yyyy", "YY", "yy", "MMM", "MM", "DD", "dd", "HH", "hh", "mm", "SS", "ss")
_LITERAL_CHARS = " $£€-+/():"


class FormatError(ValueError):
    """Raised for an unsupported or malformed format string."""


@dataclass(frozen=True, slots=True)
class _Numeric:
    prefix: str
    suffix: str
    min_int: int
    min_dec: int
    max_dec: int
    thousands: bool
    scale: int  # 100 when the format contains a percent sign, else 1


@dataclass(frozen=True, slots=True)
class _Date:
    parts: tuple[tuple[bool, str], ...]  # (is_token, text)


@lru_cache(maxsize=1024)
def _compile(fmt: str) -> _Numeric | _Date | None:
    """Compile a format string. ``None`` means General."""
    if fmt == GENERAL:
        return None
    if not fmt:
        raise FormatError("Empty format")
    parts: list[tuple[str, str]] = []  # (kind, text) with kind in lit/date/num
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == '"':
            end = fmt.find('"', i + 1)
            if end < 0:
                raise FormatError(f"Unterminated quote in {fmt!r}")
            parts.append(("lit", fmt[i + 1 : end]))
            i = end + 1
        elif tok := next((t for t in _DATE_TOKENS if fmt.startswith(t, i)), None):
            parts.append(("date", tok))
            i += len(tok)
        elif ch in "0#,.":
            parts.append(("num", ch))
            i += 1
        elif ch == "%":
            parts.append(("pct", ch))
            i += 1
        elif ch in _LITERAL_CHARS:
            parts.append(("lit", ch))
            i += 1
        else:
            raise FormatError(f"Unsupported character {ch!r} in format {fmt!r}")
    kinds = {k for k, _ in parts}
    if "date" in kinds:
        if kinds & {"num", "pct"}:
            raise FormatError(f"Cannot mix date and number codes in {fmt!r}")
        return _Date(tuple((k == "date", t) for k, t in parts))
    return _compile_numeric(fmt, parts)


def _compile_numeric(fmt: str, parts: list[tuple[str, str]]) -> _Numeric:
    idx = [n for n, (k, _) in enumerate(parts) if k == "num"]
    if not idx:
        raise FormatError(f"No digit placeholder in {fmt!r}")
    if idx != list(range(idx[0], idx[-1] + 1)):
        raise FormatError(f"Digit placeholders must be contiguous in {fmt!r}")
    pattern = "".join(parts[n][1] for n in idx)
    if pattern.count(".") > 1:
        raise FormatError(f"More than one decimal point in {fmt!r}")
    int_part, _, dec_part = pattern.partition(".")
    if "," in dec_part:
        raise FormatError(f"Thousands separator after decimal point in {fmt!r}")

    def text(seg: list[tuple[str, str]]) -> str:
        return "".join(t for _, t in seg)

    return _Numeric(
        prefix=text(parts[: idx[0]]),
        suffix=text(parts[idx[-1] + 1 :]),
        min_int=int_part.count("0"),
        min_dec=dec_part.count("0"),
        max_dec=len(dec_part),
        thousands="," in int_part,
        scale=100 if any(k == "pct" for k, _ in parts) else 1,
    )


def validate_format(fmt: str) -> None:
    """Raise FormatError if ``fmt`` is not a supported format string."""
    _compile(fmt)


def is_date_format(fmt: str) -> bool:
    try:
        return isinstance(_compile(fmt), _Date)
    except FormatError:
        return False


def format_value(value: Value, fmt: str) -> str:
    """Render a cell value for display using ``fmt``."""
    compiled = _compile(fmt)  # raises FormatError even for values that ignore the format
    if isinstance(value, float) and not math.isfinite(value):
        return "#NUM!"  # cannot happen through normal entry; must never break a view
    if isinstance(value, float) and compiled is not None:
        if isinstance(compiled, _Date):
            return _render_date(value, compiled)
        return _render_numeric(value, compiled)
    return _general(value)


def _general(value: Value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return f"{value:.10g}"
    if isinstance(value, ErrorValue):
        return str(value)
    return value


def _render_numeric(value: float, f: _Numeric) -> str:
    with localcontext() as ctx:
        ctx.prec = 400
        scaled = Decimal(repr(value)) * f.scale
        rounded = scaled.quantize(Decimal(1).scaleb(-f.max_dec), rounding=ROUND_HALF_UP)
    negative = rounded < 0
    digits = f"{abs(rounded):f}"
    int_digits, _, dec_digits = digits.partition(".")
    int_digits = "" if int_digits == "0" and f.min_int == 0 else int_digits
    int_digits = int_digits.rjust(f.min_int, "0")
    if f.thousands:
        int_digits = _group(int_digits)
    dec_digits = dec_digits.ljust(f.max_dec, "0")
    keep = len(dec_digits)
    while keep > f.min_dec and dec_digits[keep - 1] == "0":
        keep -= 1
    dec_digits = dec_digits[:keep]
    number = int_digits + ("." + dec_digits if dec_digits else "")
    return ("-" if negative else "") + f.prefix + number + f.suffix


def _group(digits: str) -> str:
    """Insert a comma between each group of three digits."""
    head = len(digits) % 3 or 3
    return ",".join(
        [digits[:head], *(digits[i : i + 3] for i in range(head, len(digits), 3))]
    ).strip(",")


def _render_date(value: float, f: _Date) -> str:
    try:
        y, mo, d, hh, mi, ss = split_serial(value)
    except ValueError:
        return "########"
    values = {
        "YYYY": f"{y:04d}",
        "yyyy": f"{y:04d}",
        "YY": f"{y % 100:02d}",
        "yy": f"{y % 100:02d}",
        "MMM": _MONTHS[mo - 1],
        "MM": f"{mo:02d}",
        "DD": f"{d:02d}",
        "dd": f"{d:02d}",
        "HH": f"{hh:02d}",
        "hh": f"{hh:02d}",
        "mm": f"{mi:02d}",
        "SS": f"{ss:02d}",
        "ss": f"{ss:02d}",
    }
    return "".join(values[t] if is_token else t for is_token, t in f.parts)
