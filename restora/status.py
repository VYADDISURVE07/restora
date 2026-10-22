"""ExpectedStatus values: an exact code such as 201, or a whole class such as 4xx (any client error)."""
from __future__ import annotations

import re


class StatusClass(int):
    """'4xx' means any status from 400 to 499.

    It behaves like the number 400 in range checks (so `200 <= s < 300` still tells success from failure)
    and prints as '4xx'. Compare it with `matches()`, never with `==`.
    """

    def __new__(cls, hundreds: int):
        return super().__new__(cls, int(hundreds) * 100)

    def matches(self, status) -> bool:
        return status is not None and int(status) // 100 == int(self) // 100

    def __str__(self) -> str:
        return f"{int(self) // 100}xx"

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


def parse_status(text: str):
    """None (not given), an int (exact code) or a StatusClass ('4xx'). Raises ValueError for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"([1-5])\s*xx", text, re.I)
    if m:
        return StatusClass(int(m.group(1)))
    m = re.match(r"^(\d{3})\b", text)
    if not m or not 100 <= int(m.group(1)) <= 599:
        raise ValueError(f"ExpectedStatus '{text}' is not an HTTP status code like 200 or 404 (or a class like 4xx)")
    return int(m.group(1))


def status_matches(expected, actual) -> bool:
    if isinstance(expected, StatusClass):
        return expected.matches(actual)
    return expected == actual


def shown(expected) -> str | int | None:
    """For JSON: exact codes stay numbers, classes become '4xx'."""
    return str(expected) if isinstance(expected, StatusClass) else expected
