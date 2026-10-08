"""Restricted Canvas HTTP client.

Allowed: GET users/self, GET the configured forum topic, GET its entries and
replies (paginated), POST a new entry, POST a reply. Everything else is rejected
before any network I/O. GETs are retried; a POST is never retried here.
"""
import logging
import random
import re
import time
from urllib.parse import parse_qsl, urlparse

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429}
USER_AGENT = "canvas-forum-agent/0.1"


class CanvasError(Exception):
    """A definite failure."""


class RejectedRequest(CanvasError):
    """Blocked locally by policy; nothing was sent."""


class HTTPFailure(CanvasError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class UnknownOutcome(CanvasError):
    """A POST may or may not have been applied. Caller must look before retrying."""


class CanvasClient:
    def __init__(self, config, token, session=None, sleep=time.sleep, rng=random.random,
                 max_retries=4, base_delay=0.5, max_delay=8.0, timeout=(5, 20), max_pages=200):
        config.require_ids()
        self._config = config
        self._token = token
        self._session = session or requests.Session()
        self._sleep = sleep
        self._rng = rng
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.max_pages = max_pages
        self._fault = config.fault  # fires once, on the first POST of this process
        base = urlparse(config.base_url)
        self._origin = (base.scheme, base.netloc)
        topic = rf"/api/v1/courses/{config.course_id}/discussion_topics/{config.topic_id}"
        self._topic_path = topic
        self._rules = [
            ("GET", re.compile(r"^/api/v1/users/self$")),
            ("GET", re.compile(rf"^{topic}$")),
            ("GET", re.compile(rf"^{topic}/entries$")),
            ("GET", re.compile(rf"^{topic}/entries/\d+/replies$")),
            ("POST", re.compile(rf"^{topic}/entries$")),
            ("POST", re.compile(rf"^{topic}/entries/\d+/replies$")),
        ]

    # --- policy -----------------------------------------------------------

    def _check(self, method, url):
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc) != self._origin:
            raise RejectedRequest("host not allowed")
        for allowed_method, pattern in self._rules:
            if method == allowed_method and pattern.match(parsed.path):
                return
        raise RejectedRequest(f"{method} {parsed.path} is not an allowed request")

    def _url(self, path):
        return self._config.base_url + path

    # --- transport --------------------------------------------------------

    def _send(self, method, url, data=None):
        self._check(method, url)
        return self._session.request(
            method, url, data=data, timeout=self.timeout, allow_redirects=False,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                     "User-Agent": USER_AGENT},
        )

    @staticmethod
    def _log_http_error(method, url, resp):
        """Log request shape and response head; never the token or Authorization header."""
        parsed = urlparse(url)
        sent = getattr(getattr(resp, "request", None), "headers", None) or {}
        try:
            body = (resp.text or "")[:200]
        except Exception:  # noqa: BLE001 - diagnostics must not mask the real error
            body = "<unreadable>"
        log.error("HTTP error: method=%s path=%s query=%s user_agent=%s status=%s body=%r",
                  method, parsed.path, parse_qsl(parsed.query, keep_blank_values=True),
                  sent.get("User-Agent", USER_AGENT), resp.status_code, body)

    def _backoff(self, attempt):
        delay = min(self.max_delay, self.base_delay * (2 ** attempt))
        self._sleep(delay * (0.5 + self._rng() / 2))

    def _get(self, url):
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._send("GET", url)
            except RejectedRequest:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = type(exc).__name__
            else:
                if resp.status_code >= 300:
                    self._log_http_error("GET", url, resp)
                if resp.status_code in RETRY_STATUS or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                elif resp.status_code >= 300:
                    raise HTTPFailure(resp.status_code, f"GET failed: HTTP {resp.status_code}")
                else:
                    return resp
            if attempt < self.max_retries:
                log.warning("GET failed (%s), retry %d/%d", last, attempt + 1, self.max_retries)
                self._backoff(attempt)
        raise CanvasError(f"GET gave up after {self.max_retries + 1} attempts ({last})")

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except ValueError as exc:
            raise CanvasError("malformed JSON in response") from exc

    def _get_paginated(self, path):
        url, out = self._url(path), []
        for _ in range(self.max_pages):
            resp = self._get(url)
            body = self._json(resp)
            if not isinstance(body, list):
                raise CanvasError("expected a JSON list")
            out.extend(body)
            url = resp.links.get("next", {}).get("url")
            if not url:
                return out
        raise CanvasError("too many pages")

    # --- reads ------------------------------------------------------------

    def get_self(self):
        return self._json(self._get(self._url("/api/v1/users/self")))

    def get_topic(self):
        return self._json(self._get(self._url(self._topic_path)))

    def list_entries(self):
        return self._get_paginated(f"{self._topic_path}/entries")

    def list_replies(self, entry_id):
        entry_id = self._digits(entry_id)
        return self._get_paginated(f"{self._topic_path}/entries/{entry_id}/replies")

    @staticmethod
    def _digits(value):
        value = str(value)
        if not value.isdigit():
            raise RejectedRequest("entry id must be numeric")
        return value

    # --- writes -----------------------------------------------------------

    def post_entry(self, message):
        return self._post(f"{self._topic_path}/entries", message)

    def post_reply(self, entry_id, message):
        entry_id = self._digits(entry_id)
        return self._post(f"{self._topic_path}/entries/{entry_id}/replies", message)

    def _post(self, path, message):
        if not isinstance(message, str) or not message.strip():
            raise RejectedRequest("empty message")
        url = self._url(path)
        self._check("POST", url)
        fault, self._fault = self._fault, ""
        if fault == "http500":  # server answers 500 without (visibly) applying the write
            raise UnknownOutcome("POST answered HTTP 500 (injected fault)")
        try:
            resp = self._send("POST", url, {"message": message})
            if fault == "duplicate":
                self._send("POST", url, {"message": message})
            if fault == "lost_ack":
                raise requests.Timeout("ack lost (injected fault)")
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise UnknownOutcome(f"POST outcome unknown: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            self._log_http_error("POST", url, resp)
        if resp.status_code >= 500:
            raise UnknownOutcome(f"POST answered HTTP {resp.status_code}")
        if resp.status_code >= 300:
            raise HTTPFailure(resp.status_code, f"POST rejected: HTTP {resp.status_code}")
        try:
            if fault == "malformed":
                raise ValueError("injected")
            body = resp.json()
            if not isinstance(body, dict) or "id" not in body:
                raise ValueError("no id")
        except ValueError as exc:
            raise UnknownOutcome("POST succeeded but the response was malformed") from exc
        return body
