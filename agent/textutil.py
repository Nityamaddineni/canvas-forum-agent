import hashlib
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "tr", "ul", "ol", "table", "pre", "blockquote",
          "h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style"}


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag == "li":  # one "- " line per item, so a list reads back as it was written
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            self.parts.append("- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "li" or tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    """Strip HTML to plain text, keeping block boundaries as newlines."""
    if not html:
        return ""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    out = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def normalize(text):
    return " ".join(text.split())


def text_hash(text):
    """Hash of already-extracted plain text, whitespace-normalised. Never re-parsed as HTML."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def message_hash(html):
    """Hash of the visible text of an HTML message (stable across Canvas HTML wrapping)."""
    return text_hash(html_to_text(html))
