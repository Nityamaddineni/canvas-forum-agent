"""Kill switch: the forum topic itself must say we may post."""
import logging
from dataclasses import dataclass

from .canvas import CanvasError
from .textutil import html_to_text

log = logging.getLogger(__name__)

CONTROL_LINE = "COURSE-TEAM CONTROL: RUNNING"


@dataclass
class ControlResult:
    ok: bool
    reason: str
    error: bool = False  # True when the topic could not be read at all


def check(client):
    """Fetch the topic fresh (never cached) and require the exact control line first."""
    try:
        topic = client.get_topic()
    except CanvasError as exc:
        return ControlResult(False, f"could not read control topic: {exc}", error=True)
    text = html_to_text(topic.get("message") if isinstance(topic, dict) else "")
    first = text.split("\n", 1)[0].strip() if text else ""
    if first == CONTROL_LINE:
        return ControlResult(True, "running")
    return ControlResult(False, "control line is not RUNNING")
