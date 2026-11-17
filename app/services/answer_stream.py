"""Read the `answer` string out of a structured reply while it is still arriving.

The assistant's replies are JSON constrained by a schema (`prompts.py`), and the
text a woman reads is one string field of it, `answer`. Streaming that JSON
verbatim would show her braces and keys; waiting for it to parse is the eight
to thirteen seconds this exists to remove. So the raw JSON text is fed in as
the model writes it and only the decoded characters of the top-level `answer`
value come out — escapes resolved, nothing emitted twice.

This is a display aid only. The reply that is stored, traced and finally sent
is still `json.loads` of the complete text, so a quirk here can make the live
text lag, never make the final answer differ.
"""

from __future__ import annotations

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


class AnswerFieldStream:
    """An incremental reader for one top-level string field of a JSON object.

    `feed(chunk)` returns the characters of the field's value decoded from that
    chunk (possibly empty). Chunks may split anywhere — inside a key, an escape
    or a `\\uXXXX` sequence — because the state is kept character by character.
    Nested objects and arrays are skipped, so an `answer` key inside a card is
    never mistaken for the reply.
    """

    def __init__(self, key: str = "answer") -> None:
        self.key = key
        self.done = False
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._unicode: str | None = None  # hex digits of a \u escape so far
        self._high_surrogate: int | None = None
        self._expect_key = False
        self._after_colon = False
        self._reading_key = False
        self._key_chars: list[str] = []
        self._last_key: str | None = None
        self._capturing = False

    def feed(self, chunk: str) -> str:
        if self.done or not chunk:
            return ""
        out: list[str] = []
        for ch in chunk:
            if self.done:
                break
            if self._in_string:
                self._string_char(ch, out)
            else:
                self._structural_char(ch)
        return "".join(out)

    # ----------------------------------------------------------- internals

    def _emit(self, text: str, out: list[str]) -> None:
        if self._capturing:
            out.append(text)
        elif self._reading_key:
            self._key_chars.append(text)

    def _flush_surrogate(self, out: list[str]) -> None:
        if self._high_surrogate is not None:
            self._emit(chr(self._high_surrogate), out)
            self._high_surrogate = None

    def _string_char(self, ch: str, out: list[str]) -> None:
        if self._unicode is not None:
            self._unicode += ch
            if len(self._unicode) < 4:
                return
            try:
                code = int(self._unicode, 16)
            except ValueError:
                code = 0xFFFD
            self._unicode = None
            if 0xD800 <= code <= 0xDBFF:
                self._flush_surrogate(out)
                self._high_surrogate = code
            elif 0xDC00 <= code <= 0xDFFF and self._high_surrogate is not None:
                pair = 0x10000 + ((self._high_surrogate - 0xD800) << 10) + (code - 0xDC00)
                self._high_surrogate = None
                self._emit(chr(pair), out)
            else:
                self._flush_surrogate(out)
                self._emit(chr(code), out)
            return
        if self._escape:
            self._escape = False
            if ch == "u":
                self._unicode = ""
                return
            self._flush_surrogate(out)
            self._emit(_SIMPLE_ESCAPES.get(ch, ch), out)
            return
        if ch == "\\":
            self._escape = True
            return
        self._flush_surrogate(out)
        if ch == '"':
            self._close_string()
            return
        self._emit(ch, out)

    def _close_string(self) -> None:
        self._in_string = False
        if self._reading_key:
            self._reading_key = False
            self._last_key = "".join(self._key_chars)
            self._key_chars = []
        elif self._capturing:
            self._capturing = False
            self.done = True

    def _structural_char(self, ch: str) -> None:
        if ch == '"':
            self._in_string = True
            if self._depth == 1 and self._expect_key:
                self._expect_key = False
                self._reading_key = True
            elif self._depth == 1 and self._after_colon:
                self._after_colon = False
                self._capturing = self._last_key == self.key
            return
        if ch in "{[":
            if self._depth == 0 and ch == "{":
                self._expect_key = True
            self._after_colon = False
            self._depth += 1
        elif ch in "}]":
            self._depth -= 1
        elif self._depth == 1:
            if ch == ":":
                self._after_colon = True
            elif ch == ",":
                self._expect_key = True
                self._after_colon = False
