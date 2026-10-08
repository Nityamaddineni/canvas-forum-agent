import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

KEYCHAIN_ITEM = "canvas-hw3-token"
FAULTS = ("lost_ack", "duplicate", "http500", "malformed")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_BANNED_PHRASES = Path(__file__).resolve().parent.parent / "prompts" / "banned_phrases.txt"


class ConfigError(Exception):
    pass


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    course_id: str = ""
    topic_id: str = ""
    base_url: str = "https://canvas.mit.edu"
    dry_run: bool = False
    fault: str = ""
    state_dir: Path = Path("state")
    banned_phrases_path: Path = DEFAULT_BANNED_PHRASES

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        base = (env.get("BASE_URL") or "https://canvas.mit.edu").strip().rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
        ):
            raise ConfigError("BASE_URL must be https (http allowed only for localhost)")
        if not parsed.hostname:
            raise ConfigError("BASE_URL has no host")
        fault = (env.get("FAULT") or "").strip().lower()
        if fault and fault not in FAULTS:
            raise ConfigError(f"FAULT must be one of {', '.join(FAULTS)}")
        return cls(
            course_id=(env.get("COURSE_ID") or "").strip(),
            topic_id=(env.get("TOPIC_ID") or "").strip(),
            base_url=base,
            dry_run=_truthy(env.get("DRY_RUN")),
            fault=fault,
            state_dir=Path(env.get("STATE_DIR") or "state"),
            banned_phrases_path=Path(env.get("BANNED_PHRASES_FILE") or DEFAULT_BANNED_PHRASES),
        )

    def require_ids(self):
        if not (self.course_id.isdigit() and self.topic_id.isdigit()):
            raise ConfigError("COURSE_ID and TOPIC_ID must be set to numeric ids")

    @property
    def db_path(self):
        return self.state_dir / "agent.db"

    @property
    def stopped_path(self):
        return self.state_dir / "STOPPED"


def get_token(env=None):
    """CANVAS_TOKEN, else the macOS Keychain item. The value is registered for log redaction."""
    env = os.environ if env is None else env
    token = (env.get("CANVAS_TOKEN") or "").strip()
    if not token and sys.platform == "darwin":
        for flag in ("-s", "-a"):
            try:
                proc = subprocess.run(
                    ["security", "find-generic-password", flag, KEYCHAIN_ITEM, "-w"],
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0 and proc.stdout.strip():
                token = proc.stdout.strip()
                break
    if not token:
        raise ConfigError(f"no Canvas token: set CANVAS_TOKEN or Keychain item {KEYCHAIN_ITEM}")
    register_secret(token)
    return token


# --- log redaction -------------------------------------------------------

_SECRETS = set()
_PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b\d{2,6}~[A-Za-z0-9]{16,}\b"),
]


def register_secret(value):
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text):
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record):
        message = redact(record.getMessage())
        if record.exc_info:
            exc = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(exc)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        record.msg, record.args = message, ()
        return True


def setup_logging(level="INFO"):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_agent_handler", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._agent_handler = True
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
