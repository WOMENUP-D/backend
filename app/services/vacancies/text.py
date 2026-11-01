"""Text handling shared by vacancy providers: an allowlist HTML sanitiser.

A board's description is the employer's own HTML. The page shows it as the
employer structured it — headings, paragraphs, bullet lists — and nothing
else: no scripts, no styles, no attributes, no iframes, no images. Anything
outside the allowlist loses its tag and keeps its text, so a description is
never emptied by an unexpected element.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser

ALLOWED = frozenset({"p", "br", "ul", "ol", "li", "strong", "em", "h3", "h4", "h5"})
#: Tags rewritten to an allowed one.
RENAMED = {"b": "strong", "i": "em", "h1": "h3", "h2": "h3", "h6": "h5"}
#: Elements whose content is not text at all.
DROPPED = frozenset({"script", "style", "iframe", "object", "embed", "svg", "noscript"})
VOID = frozenset({"br"})


class _Sanitiser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = RENAMED.get(tag, tag)
        if tag in DROPPED:
            self.skipping += 1
            return
        if self.skipping or tag not in ALLOWED:
            return
        if tag in VOID:
            self.parts.append("<br>")
            return
        # A list item or a paragraph opened while one is still open closes
        # it, as a browser would — otherwise a lazily written `<li>Один<li>Два`
        # nests forever.
        if tag in ("li", "p") and self.open and self.open[-1] == tag:
            self.open.pop()
            self.parts.append(f"</{tag}>")
        self.parts.append(f"<{tag}>")
        self.open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = RENAMED.get(tag, tag)
        if tag in DROPPED:
            self.skipping = max(0, self.skipping - 1)
            return
        if self.skipping or tag not in ALLOWED or tag in VOID or tag not in self.open:
            return
        # Close everything opened since, so the output is well formed even
        # when the input was not.
        while self.open:
            last = self.open.pop()
            self.parts.append(f"</{last}>")
            if last == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.open:
            self.parts.append(f"</{self.open.pop()}>")
        out = "".join(self.parts)
        # Empty wrappers an editor leaves behind.
        for tag in ("p", "li", "strong", "em"):
            out = out.replace(f"<{tag}></{tag}>", "")
        return out.strip()


def sanitize_html(markup: str | None) -> str:
    """The employer's description, reduced to the allowlist. Plain text that
    was never HTML comes back as one paragraph."""
    if not markup:
        return ""
    parser = _Sanitiser()
    parser.feed(markup)
    parser.close()
    out = parser.result()
    if out and "<" not in out:
        out = f"<p>{out}</p>"
    return out
