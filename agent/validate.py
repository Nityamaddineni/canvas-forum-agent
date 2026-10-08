import difflib
import re

from .compose import SIGNATURE, count_sentences, parse_blocks, strip_signature
from .config import DEFAULT_BANNED_PHRASES
from .textutil import normalize

MAX_MESSAGE_CHARS = 1200
MAX_SENTENCES = 10
MAX_BULLETS = 6
SIMILARITY_LIMIT = 0.85

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
_BARE = re.compile(
    r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|io|co|ly|me|xyz|info|app|dev|ai|us|uk|ru|cn|tk|ml)\b"
    r"(?:/[^\s<>\"')\]]*)?")
_MD_LINK = re.compile(r"\]\(")
_SECRET_PATTERNS = [
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"\b\d{2,6}~[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_ -]?key|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bCANVAS_TOKEN\b|canvas-hw3-token"),
    re.compile(r"\b[A-Za-z0-9_\-+/=]{32,}\b"),
]

_DASH = re.compile("[‒-―⸺⸻]")  # figure, en, em, horizontal bar, two/three-em
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u231a\u231b\u23e9-\u23ff\u25aa\u25ab\u25b6\u25c0\u25fb-\u25fe"
    "\u2b05-\u2b07\u2b1b\u2b1c\u2b50\u2b55\u2934\u2935\u3030\u303d\u3297\u3299\u200d\ufe0f\u20e3]")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_BOLD = re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__")
_TABLE = re.compile(r"(?m)^\s*\|.*\|\s*$|^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_OTHER_BULLET = re.compile(r"(?m)^\s*[*+•·▪●]\s+\S")
_SIGNATURE = re.compile(r"(?i)\bnitya['’]?s\s+ai\s+agent\b")
_OPENER = re.compile(
    r"(?i)^\W*(?:great (?:point|question|idea)|good (?:point|question|idea)|nice (?:point|question)"
    r"|excellent (?:point|question)|i agree|agreed|thanks|thank you|interesting|absolutely|exactly)\b")
_TOKEN = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*")


def load_banned_phrases(path=None):
    """[(pattern, ...)] from the editable list. Raises OSError or ValueError if unusable."""
    patterns = []
    for n, line in enumerate(open(path or DEFAULT_BANNED_PHRASES, encoding="utf-8"), 1):
        line = line.strip().replace("’", "'")
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("re:"):
                patterns.append(re.compile(line[3:], re.I))
            else:
                words = r"\s+".join(re.escape(w) for w in line.split())
                patterns.append(re.compile(rf"(?<!\w){words}(?!\w)", re.I))
        except re.error as exc:
            raise ValueError(f"line {n}: {exc}") from exc
    return patterns


def _name_problems(text, names):
    """Any full or last name is rejected; a first name only as "<First>'s agent"."""
    full, lasts, firsts = {}, set(), set()
    for name in names:
        tokens = [t for t in _TOKEN.findall(name.replace("’", "'")) if len(t) > 1]
        if not tokens or name.strip().lower() == "unknown":
            continue
        if len(tokens) > 1:
            full[" ".join(tokens)] = tokens
            lasts.add(tokens[-1])
            firsts.update(tokens[:-1])
        else:
            firsts.add(tokens[0])
    firsts -= lasts
    text = text.replace("’", "'")
    problems = []
    for display, tokens in sorted(full.items()):
        if re.search(r"(?<!\w)" + r"\s+".join(map(re.escape, tokens)) + r"(?!\w)", text, re.I):
            problems.append(f'message contains the full name "{display}"; remove it')
    for last in sorted(lasts, key=str.lower):
        if re.search(rf"(?<!\w){re.escape(last)}(?!\w)", text, re.I):
            problems.append(f'message contains the last name "{last}"; remove it')
    for first in sorted(firsts, key=str.lower):
        if re.search(rf"(?<!\w){re.escape(first)}(?!\w)(?!'s\s+agent(?!\w))", text, re.I):
            problems.append(f'"{first}" is allowed only as "{first}\'s agent"')
    return problems


def validate_message(message, *, token, past_posts, author_names=(), banned_phrases_path=None):
    """Return a list of problems; empty means the message is acceptable."""
    problems = []
    text = message.strip()
    if not text:
        return ["message is empty"]
    if len(text) > MAX_MESSAGE_CHARS:
        problems.append(f"message longer than {MAX_MESSAGE_CHARS} characters ({len(text)})")

    blocks = parse_blocks(text)
    sentences = count_sentences(blocks)
    if sentences > MAX_SENTENCES:
        problems.append(f"message has {sentences} sentences (max {MAX_SENTENCES}; each bullet counts as one)")
    bullets = sum(len(v) for k, v in blocks if k == "ul")
    if bullets > MAX_BULLETS:
        problems.append(f"message has {bullets} bullets (max {MAX_BULLETS})")
    if any(k == "ul" and (i == 0 or blocks[i - 1][0] != "p") for i, (k, _) in enumerate(blocks)):
        problems.append("a bullet list must come after an intro sentence")
    if _OTHER_BULLET.search(text):
        problems.append('bullets must start with "- "')

    if _DASH.search(text):
        problems.append("message contains an em dash or en dash; use a comma, colon or new sentence")
    if _EMOJI.search(text):
        problems.append("message contains an emoji or symbol character")
    scan = _EMAIL.sub(" ", text)
    if _URL.search(text) or _BARE.search(scan) or _MD_LINK.search(text):
        problems.append("message contains a link or web address")
    if _EMAIL.search(text):
        problems.append("message contains an email address")
    if _HEADING.search(text):
        problems.append("message contains a markdown heading")
    if _BOLD.search(text):
        problems.append("message contains markdown bold")
    if _TABLE.search(text):
        problems.append("message contains a table")
    if _SIGNATURE.search(text):
        problems.append("message contains a signature; the tool adds it")

    problems += _name_problems(text, author_names)

    straight = text.replace("’", "'")
    opener = _OPENER.match(straight)
    if opener:
        problems.append(f'message opens with filler or praise ("{opener.group(0).strip()}")')
    try:
        banned = load_banned_phrases(banned_phrases_path)
    except (OSError, ValueError) as exc:
        problems.append(f"banned phrase list unusable ({exc}); not posting")
        banned = []
    for pattern in banned:
        found = pattern.search(straight)
        if found:
            problems.append(f'banned phrase: "{found.group(0)}"')

    if (token and token in message) or any(p.search(text) for p in _SECRET_PATTERNS):
        problems.append("message looks like it contains a secret")

    norm = normalize(text).lower()
    for past in past_posts:
        past_norm = normalize(strip_signature(past)).lower()
        if past_norm and difflib.SequenceMatcher(None, norm, past_norm).ratio() >= SIMILARITY_LIMIT:
            problems.append("message is too similar to one of my past posts")
            break
    return problems
