"""Names are shown as names, whatever case they were typed in."""

from __future__ import annotations

import pytest

from app.core.text import tidy_name


@pytest.mark.parametrize(
    ("typed", "shown"),
    [
        ("durdona kim", "Durdona Kim"),
        ("DURDONA KIM", "Durdona Kim"),
        ("  dilnoza   karimova ", "Dilnoza Karimova"),
        ("anna-maria", "Anna-Maria"),
        ("o'tkir", "O'tkir"),
        ("gʻulom", "Gʻulom"),
        ("sa'dulla", "Sa'dulla"),
        ("Oʻtkir Yusupov", "Oʻtkir Yusupov"),
        ("McDonald", "McDonald"),
        ("Dilnoza Karimova", "Dilnoza Karimova"),
        ("a", "A"),
        ("durdona (demo)", "Durdona (Demo)"),
        ("--", "--"),
    ],
)
def test_a_name_is_written_as_a_name(typed: str, shown: str) -> None:
    assert tidy_name(typed) == shown


def test_blank_input_is_no_name() -> None:
    assert tidy_name(None) is None
    assert tidy_name("   ") is None
