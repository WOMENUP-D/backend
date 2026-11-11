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
#: Marks people, keyboards and models substitute for ʻ in oʻ / gʻ: the left
#: quote, the right quote, the plain apostrophe and the modifier apostrophe ʼ
#: itself (models write "koʼnikma" as often as "koʻnikma"). After an o or a g
#: any of them means the letter — Uzbek has no glottal stop straight after o
#: or g; elsewhere a quote or apostrophe is the glottal stop (taʼlim,
#: maʼlumot) and is left alone.
OKINA_LIKE = OKINA + TUTUQ + "‘’'"

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
        # "qöʼlda": the new letter *and* the old mark — the mark is a leftover.
        (f"([ÖöĞğ])[{OKINA_LIKE}]", r"\1"),
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


#: Cyrillic letters to the reformed Latin, for the one case where a Cyrillic
#: letter is certainly a slip: inside a word that is otherwise Latin
#: ("bölади", "köniкmalar" — model output mixes scripts mid-word).
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ç", "ш": "ş", "щ": "şç", "ъ": TUTUQ,
    "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "ö", "қ": "q",
    "ғ": "ğ", "ҳ": "h",
}  # fmt: skip
#: Letters of neighbouring alphabets that are not Uzbek and that models slip
#: into Uzbek words: Turkish ü and dotless ı, and ñ for the ng sound.
_NOT_UZBEK = {"ü": "u", "Ü": "U", "ı": "i", "ñ": "ng", "Ñ": "Ng"}
_ANY_WORD = re.compile(r"[^\W\d_][^\W_]*", re.UNICODE)
_LATIN_LETTER = re.compile(r"[A-Za-zÖöĞğŞşÇç]")
_NOT_UZBEK_LETTER = re.compile("[üÜıñÑ]")


def _cyrillic_letter(letter: str) -> str:
    latin = _CYRILLIC_TO_LATIN.get(letter.lower())
    if latin is None:
        return letter
    return latin.capitalize() if letter.isupper() else latin


def _mend_word(match: re.Match[str]) -> str:
    word = match.group(0)
    if _CYRILLIC.search(word) and _LATIN_LETTER.search(word):
        word = "".join(_cyrillic_letter(ch) if _CYRILLIC.match(ch) else ch for ch in word)
    if _NOT_UZBEK_LETTER.search(word):
        word = "".join(_NOT_UZBEK.get(ch, ch) for ch in word)
    return word


def mend_script(text: str | None) -> str:
    """Repair the script slips model-written Uzbek makes inside a word.

    A Cyrillic letter inside an otherwise Latin word, and the Turkish ü / ı or
    ñ, are never right in Uzbek Latin. Whole Cyrillic words are left alone —
    a Russian name quoted in an Uzbek sentence is not a slip.
    """
    if not text:
        return text or ""
    return _ANY_WORD.sub(_mend_word, text)


def to_new_alphabet(text: str | None) -> str:
    """Uzbek text in the reformed alphabet, whatever alphabet it arrived in."""
    if not text:
        return text or ""
    return _map_words(mend_script(text), _convert_word)


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


# --- which language a question is written in --------------------------------

#: Letters only Uzbek Cyrillic has.
_UZBEK_CYRILLIC = re.compile("[ЎўҚқҒғҲҳ]")
_LATIN_WORD = re.compile(r"[A-Za-zÖöĞğŞşÇçʻʼ‘’'-]+")
#: Uzbek words a question is made of that `_UZBEK_HINTS` does not list.
_UZBEK_QUESTION_WORDS = frozenset(
    "qanday qanaqa qaysi qayerda qayerdan qachon nima nimaga nega necha kim men menga "  # noqa: SIM905
    "mening meni sizga sizning bormi yoqmi mumkinmi kerakmi qilsam bolsam olsam "
    "qilaman olaman bolaman boladi qiladi kurs kurslar dastur dasturlar portal "
    "portalda sertifikat ish ishga oqish oqishni organish yordam salom rahmat "
    "iltimos haqida uchun uçun".split()
)
#: Suffixes that mark a Latin word as Uzbek: possessive, case, plural,
#: the question particle and first-person endings.
_UZBEK_SUFFIX = re.compile(
    r"(?:ning|ni|ga|da|dan|lar|larni|larga|imni|ingiz|imiz|miz|sizmi|mi|man|aman|yapman|"
    r"yapti|moqda|gan|dagi)$"
)
_ENGLISH_WORDS = frozenset(
    "the a an and or of to in on for with from by is are was were be been am do does did "  # noqa: SIM905
    "how what which where when why who whom can could should would will i my me you your "
    "it this that these those there get have has please help course courses certificate "
    "reset password become want need about any some there's i'm".split()
)


def fold(word: str) -> str:
    """Lower case, the reformed letters back to their old base form and every
    apostrophe removed — so oʻzimni / o'zimni / ozimni / özimni all agree.
    For keyword matching (the assistant router, search stop-words), not for
    display."""
    word = word.lower()
    for new, old in (("ö", "o"), ("ğ", "g"), ("ş", "sh"), ("ç", "ch")):
        word = word.replace(new, old)
    return re.sub("[ʻʼ‘’'`]", "", word)


def question_language(text: str | None) -> str | None:
    """The language a question is written in — uz, ru or en — or None.

    The interface locale is not the question's language: a woman reading the
    portal in Uzbek may type in Russian, and answering her in Uzbek from Uzbek
    sources only is how a question with an answer came back "not found".
    `None` means "too short or too mixed to tell", and the caller keeps the
    interface locale.
    """
    if not text:
        return None
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(re.findall("[A-Za-zÖöĞğŞşÇç]", text))
    if cyrillic and cyrillic >= latin:
        return "uz" if _UZBEK_CYRILLIC.search(text) else "ru"
    if not latin:
        return None
    if re.search("[ʻÖöĞğŞşÇç]", text):
        return "uz"
    words = [w for w in (fold(m) for m in _LATIN_WORD.findall(text)) if w]
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    uzbek = len(_UZBEK_HINTS.findall(text)) + sum(
        1
        for w in words
        if w in _UZBEK_QUESTION_WORDS
        or (len(w) > 4 and _UZBEK_SUFFIX.search(w) and w not in _ENGLISH_WORDS)
        or re.search("q(?!u)", w)
    )
    if english > uzbek and english >= 1:
        return "en"
    if uzbek > english and uzbek >= 1:
        return "uz"
    return None
