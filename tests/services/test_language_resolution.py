"""Which language a generated answer is written in.

The portal shipped with every AI roadmap in Uzbek regardless of the locale the
reader was using. The cause was not the prompt: `generate_plan` asked the
database for `User.language`, and nothing in the application ever wrote that
column, so it was the `uz` default for every account.

The fix makes the *request* authoritative and leaves the stored column as the
fallback. These tests pin that order, because the failure it replaces was
silent — the plan was perfectly good prose, in the wrong language.
"""

import pytest

from app.core.constants import Language, normalise_language


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("uz", "uz"),
        ("ru", "ru"),
        ("en", "en"),
        # The browser has a fourth locale. It is Uzbek in Cyrillic script,
        # transliterated in the browser, so the server writes plain Uzbek for
        # it rather than treating it as a language of its own.
        ("uz-Cyrl", "uz"),
        ("uz-Latn", "uz"),
        ("UZ-CYRL", "uz"),
        # Region subtags are not languages.
        ("ru-RU", "ru"),
        ("en-GB", "en"),
        ("  RU  ", "ru"),
    ],
)
def test_recognised_locales_fold_to_a_written_language(value: str, expected: str) -> None:
    assert normalise_language(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "fr", "de", "zz", "-", "uzbek-ish-nonsense!"])
def test_unrecognised_input_returns_none_rather_than_uzbek(value: str | None) -> None:
    """`None` means "no answer", which is not the same as "Uzbek".

    The caller falls through to the stored language on `None`. If this returned
    `"uz"` instead, an unparsable locale tag would silently reinstate exactly
    the bug the parameter exists to fix — and it would look like a deliberate
    choice rather than a default.
    """
    assert normalise_language(value) is None


def test_every_written_language_round_trips() -> None:
    """Adding a language to the enum must not leave this helper behind."""
    for member in Language:
        assert normalise_language(member.value) == member.value
