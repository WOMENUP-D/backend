"""Uzbek Latin script as the portal writes it: the reformed alphabet.

The reform of the Uzbek Latin alphabet replaced four digraphs with single
letters — Oʻ oʻ → Ö ö, Gʻ gʻ → Ğ ğ, Sh sh → Ş ş, Ch ch → Ç ç. Everything the
portal shows in Uzbek is written that way. The catalogue and the content are
converted once in the source; text that arrives from elsewhere — model output,
records written before the change, a search term typed from an old keyboard —
is converted on the way through by the functions here.

Two rules keep the conversion honest:

* **Only Uzbek words change.** A word that cannot be Uzbek is left alone: one
  with a w, a c that is not part of ch, ph, th or ck, a capital inside it
  (ChatGPT, WomanUP), or a name on the protected list. English and Russian
  text never contains the four digraphs in Uzbek roles, but English contains
  "sh" and "ch" everywhere, so callers pass Uzbek text, not mixed text.
* **Case follows the word.** "Shahar" → "Şahar", "SHAHAR" → "ŞAHAR",
  "Oʻzbekiston" → "Özbekiston".

`to_old_alphabet` is the inverse, for matching a search term against records
still stored in the old spelling.
"""

from __future__ import annotations

import re

OKINA = "ʻ"  # ʻ — the letter mark in the old oʻ and gʻ
TUTUQ = "ʼ"  # ʼ — the glottal stop, unchanged by the reform
#: Typographic marks people and keyboards substitute for ʻ in oʻ / gʻ.
OKINA_LIKE = OKINA + "‘"

#: A word that cannot be Uzbek: left untouched.
_FOREIGN = re.compile(r"[wW]|[cC](?![hH])|[pP][hH]|[tT][hH]|[cC][kK]|(?<=[a-z])[A-Z]")
#: Names the heuristics would not catch.
PROTECTED = frozenset({"charpentier", "shopify", "chrome", "chanel", "porsche", "bosch"})

_WORD = re.compile(r"[A-Za-zÖöĞğŞşÇç][A-Za-z0-9ÖöĞğŞşÇçʻʼ‘’'-]*")
#: Addresses survive untouched: converting "womanup.uz/dasturlar" breaks it.
_VERBATIM = re.compile(
    r"(?:https?://|www\.)[^\s,;]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b[A-Za-z0-9-]+\.(?:uz|com|org|net|io|ru|gov|edu)\b"
)

_OLD_TO_NEW: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
        (f"O[{OKINA_LIKE}]", "Ö"),
        (f"o[{OKINA_LIKE}]", "ö"),
        (f"G[{OKINA_LIKE}]", "Ğ"),
        (f"g[{OKINA_LIKE}]", "ğ"),
        ("S[hH]", "Ş"),
        ("sh", "ş"),
        ("C[hH]", "Ç"),
        ("ch", "ç"),
    )
)

_NEW = re.compile("[ÖöĞğŞşÇç]")


def is_foreign(word: str) -> bool:
    return word.lower().strip("'’") in PROTECTED or _FOREIGN.search(word) is not None


#: Uzbek abbreviations the foreign-word heuristic would mistake for brands.
ABBREVIATIONS = {"MChJ": "MÇJ", "AQSh": "AQŞ", "ShHM": "ŞHM"}


def _convert_word(word: str) -> str:
    if word in ABBREVIATIONS:
        return ABBREVIATIONS[word]
    if is_foreign(word):
        return word
    for pattern, repl in _OLD_TO_NEW:
        word = pattern.sub(repl, word)
    return word


def _map_words(text: str, convert) -> str:
    out: list[str] = []
    last = 0
    for address in _VERBATIM.finditer(text):
        out.append(_WORD.sub(lambda m: convert(m.group(0)), text[last : address.start()]))
        out.append(address.group(0))
        last = address.end()
    out.append(_WORD.sub(lambda m: convert(m.group(0)), text[last:]))
    return "".join(out)


def to_new_alphabet(text: str | None) -> str:
    """Uzbek text in the reformed alphabet, whatever alphabet it arrived in."""
    if not text:
        return text or ""
    return _map_words(text, _convert_word)


def has_old_digraphs(text: str | None) -> bool:
    """Whether a text still carries oʻ / gʻ / sh / ch in Uzbek words."""
    return bool(text) and to_new_alphabet(text) != text


def _old_word(word: str) -> str:
    if is_foreign(word):
        return word

    def digraph(match: re.Match[str]) -> str:
        letter = match.group(0)
        start, end = match.start(), match.end()
        following = word[end : end + 1]
        preceding = word[start - 1 : start] if start else ""
        caps = (following and following.isupper()) or (
            preceding.isupper() and not following.islower() and following != ""
        )
        if letter == "Ş":
            return "SH" if caps else "Sh"
        if letter == "Ç":
            return "CH" if caps else "Ch"
        return {
            "ş": "sh",
            "ç": "ch",
            "Ö": "O" + OKINA,
            "ö": "o" + OKINA,
            "Ğ": "G" + OKINA,
            "ğ": "g" + OKINA,
        }[letter]

    return _NEW.sub(digraph, word)


def to_old_alphabet(text: str | None) -> str:
    """The 1995 spelling, for matching against records written in it."""
    if not text:
        return text or ""
    return _map_words(text, _old_word)


_UZBEK_HINTS = re.compile(
    r"[ʻʼ]|[ÖöĞğŞşÇç]|\b(?:va|bilan|uchun|uçun|ham|emas|yoki|kerak|boʻl|böl|qil|yaxshi|"
    r"siz|biz|bu|shu|şu|har|hech|heç|lekin|ammo|agar)\b",
    re.I,
)
_CYRILLIC = re.compile("[Ѐ-ӿ]")


def looks_uzbek(text: str | None) -> bool:
    """A cheap guess for text whose language nobody declared: Latin script
    with Uzbek letters or a few of its function words."""
    if not text or _CYRILLIC.search(text):
        return False
    # One letter no other language here uses is decisive; otherwise two words.
    if re.search("[\u02bb\u02bcÖöĞğŞşÇç]", text):
        return True
    return len(_UZBEK_HINTS.findall(text)) >= 2


def convert_if_uzbek(text: str | None, language: str | None) -> str:
    """The reformed alphabet when the text is Uzbek — declared, or evidently."""
    if not text:
        return text or ""
    lang = (language or "").lower().split("-", 1)[0]
    if lang == "uz" or (not lang and looks_uzbek(text)):
        return to_new_alphabet(text)
    return text
