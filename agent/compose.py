"""The plain-text message format: blocks, sentence counting, and conversion to safe Canvas HTML.

One parser (parse_blocks) feeds both validation and rendering, so what is checked is what is posted.
"""
import html
import re

SIGNATURE = "Nitya's AI agent"

_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|cf|fig|eq|approx)\.", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")


def parse_blocks(text):
    """Split into ("p", text) and ("ul", [items]) blocks.

    Blank lines separate blocks; consecutive "- " lines form one list; other consecutive
    lines form one paragraph (joined with spaces).
    """
    blocks, para, items = [], [], []

    def flush_para():
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    def flush_list():
        if items:
            blocks.append(("ul", list(items)))
            items.clear()

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
        elif line.startswith("- "):
            flush_para()
            items.append(line[2:].strip())
        else:
            flush_list()
            para.append(line.strip())
    flush_para()
    flush_list()
    return blocks


def count_sentences(blocks):
    """Sentences in paragraphs, plus one per bullet."""
    total = 0
    for kind, value in blocks:
        if kind == "ul":
            total += len(value)
        else:
            protected = _ABBREV.sub(lambda m: m.group(1).replace(".", "\x00") + "\x00", value)
            total += len([s for s in _SENTENCE_END.split(protected) if s.strip()])
    return total


def to_html(text):
    """Escape every character, then wrap: blocks become <p>, bullet runs become one <ul>."""
    out = []
    for kind, value in parse_blocks(text):
        if kind == "ul":
            out.append("<ul>" + "".join(f"<li>{html.escape(i, quote=True)}</li>" for i in value) + "</ul>")
        else:
            out.append(f"<p>{html.escape(value, quote=True)}</p>")
    return "".join(out)


def add_signature(text):
    """The signature, as its own last paragraph. Called only after validation."""
    return text.strip() + "\n\n" + SIGNATURE


def strip_signature(text):
    """Remove a trailing signature paragraph (for comparing against past posts)."""
    t = text.rstrip()
    if t.replace("’", "'").endswith(SIGNATURE):
        t = t[:-len(SIGNATURE)].rstrip()
    return t
