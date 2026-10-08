import logging

import pytest
import requests

from agent import config, control, limits
from agent.canvas import CanvasClient, CanvasError, HTTPFailure, RejectedRequest, UnknownOutcome
from agent.config import Config, ConfigError
from agent.memory import Memory
from agent.textutil import html_to_text
from agent.validate import validate_message
from tests.mock_canvas import COURSE, TOKEN, TOPIC


def client(mock, **kw):
    cfg = Config(course_id=COURSE, topic_id=TOPIC, base_url=mock.url)
    sleeps = []
    c = CanvasClient(cfg, TOKEN, sleep=sleeps.append, rng=lambda: 1.0, **kw)
    c.sleeps = sleeps
    return c


# --- config ---
def test_token_from_env_and_not_in_repr():
    assert config.get_token({"CANVAS_TOKEN": "abc123456"}) == "abc123456"
    assert "abc123456" not in repr(Config.from_env({"CANVAS_TOKEN": "abc123456"}))


def test_token_missing(monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ConfigError):
        config.get_token({})


def test_bad_fault_and_insecure_base():
    with pytest.raises(ConfigError):
        Config.from_env({"FAULT": "nope"})
    with pytest.raises(ConfigError):
        Config.from_env({"BASE_URL": "http://evil.example"})


def test_redaction_filter(capsys):
    config.setup_logging()
    config.register_secret("supersecretvalue")
    logging.getLogger("t").error("tok=supersecretvalue Bearer abc.def-123")
    try:
        raise ValueError("supersecretvalue")
    except ValueError:
        logging.getLogger("t").exception("boom")
    err = capsys.readouterr().err
    assert "supersecretvalue" not in err and "abc.def-123" not in err and "[REDACTED]" in err


# --- canvas client ---
def test_allowed_reads_and_pagination(mock):
    for i in range(5):
        mock.add(7, "A", f"m{i}")
    c = client(mock)
    assert c.get_self()["id"] == 100
    assert "message" in c.get_topic()
    assert len(c.list_entries()) == 5  # three pages


def test_rejects_other_methods_paths_ids_hosts(mock):
    c = client(mock)
    for method, url in [
        ("DELETE", f"{mock.url}/api/v1/users/self"),
        ("PUT", f"{mock.url}/api/v1/users/self"),
        ("GET", f"{mock.url}/api/v1/courses/{COURSE}/discussion_topics/999"),
        ("GET", f"{mock.url}/api/v1/courses/999/discussion_topics/{TOPIC}"),
        ("GET", f"{mock.url}/api/v1/courses/{COURSE}/files"),
        ("POST", f"{mock.url}/api/v1/users/self"),
        ("GET", "http://example.com/api/v1/users/self"),
    ]:
        with pytest.raises(RejectedRequest):
            c._send(method, url)
    with pytest.raises(RejectedRequest):
        c.list_replies("1/../../x")
    assert mock.log == []


def test_get_retries_with_backoff_then_succeeds(mock):
    mock.get_failures = [500, 429, 503]
    c = client(mock)
    assert c.get_self()["id"] == 100
    assert c.sleeps == [0.5, 1.0, 2.0]  # exponential; rng=1.0 gives full jitter factor


def test_get_gives_up(mock):
    mock.get_failures = [500] * 10
    with pytest.raises(CanvasError):
        client(mock, max_retries=2).get_self()
    assert len(mock.posts("GET")) == 3


def test_get_4xx_not_retried(mock):
    c = client(mock)
    c._token = "wrong"
    with pytest.raises(HTTPFailure):
        c.get_self()
    assert len(mock.posts("GET")) == 1


def test_get_retries_timeouts():
    cfg = Config(course_id=COURSE, topic_id=TOPIC, base_url="http://127.0.0.1:9")

    class S:
        calls = 0

        def request(self, *a, **k):
            S.calls += 1
            raise requests.Timeout()

    c = CanvasClient(cfg, TOKEN, session=S(), sleep=lambda s: None, max_retries=2)
    with pytest.raises(CanvasError):
        c.get_self()
    assert S.calls == 3


def test_post_500_is_unknown_and_not_retried(mock):
    mock.post_status = 500
    with pytest.raises(UnknownOutcome):
        client(mock).post_entry("hello")
    assert len(mock.posts()) == 1


def test_post_timeout_is_unknown():
    cfg = Config(course_id=COURSE, topic_id=TOPIC, base_url="http://127.0.0.1:9")

    class S:
        calls = 0

        def request(self, *a, **k):
            S.calls += 1
            raise requests.Timeout()

    with pytest.raises(UnknownOutcome):
        CanvasClient(cfg, TOKEN, session=S()).post_entry("hi")
    assert S.calls == 1


def test_post_4xx_is_definite(mock):
    mock.post_status = 403
    with pytest.raises(HTTPFailure):
        client(mock).post_entry("hello")


# --- control ---
@pytest.mark.parametrize("msg,ok", [
    ("COURSE-TEAM CONTROL: RUNNING", True),
    ("<p>COURSE-TEAM CONTROL: RUNNING</p><p>rest</p>", True),
    ("<div>&nbsp;COURSE-TEAM CONTROL: RUNNING<br>more</div>", True),
    ("COURSE-TEAM CONTROL: PAUSED", False),
    ("COURSE-TEAM CONTROL: RUNNING NOT", False),
    ("course-team control: running", False),
    ("<p>hello</p><p>COURSE-TEAM CONTROL: RUNNING</p>", False),
    ("", False),
])
def test_control_line(mock, msg, ok):
    mock.topic_message = msg
    assert control.check(client(mock)).ok is ok


def test_control_unreadable_is_not_ok(mock):
    mock.get_failures = [500] * 10
    res = control.check(client(mock, max_retries=0))
    assert not res.ok and res.error


# --- memory / limits ---
def test_unique_intent_key(tmp_path):
    m = Memory(tmp_path / "a.db")
    m.create_action("k", "reply", "1", "x", "h")
    with pytest.raises(Exception):
        m.create_action("k", "reply", "1", "x", "h")


def test_rate_limit(tmp_path):
    t = [1000.0]
    m = Memory(tmp_path / "a.db", clock=lambda: t[0])
    for _ in range(3):
        assert limits.check_can_post(m, t[0], 0)[0]
        m.record_post()
        t[0] += 60
    assert not limits.check_can_post(m, t[0], 0)[0]
    assert limits.check_can_post(m, t[0] + 3600, 0)[0]
    assert not limits.check_can_post(m, t[0] + 3600, 1)[0]


def test_stopped_after_three_failed_runs(tmp_path):
    cfg = Config(state_dir=tmp_path)
    m = Memory(tmp_path / "agent.db")
    for outcome in ("failed", "failed"):
        m.finish_run(m.start_run("submit"), outcome)
    assert not limits.after_run(m, cfg)
    m.finish_run(m.start_run("submit"), "failed")
    assert limits.after_run(m, cfg) and cfg.stopped_path.exists()


def test_skipped_run_breaks_failure_streak(tmp_path):
    cfg = Config(state_dir=tmp_path)
    m = Memory(tmp_path / "agent.db")
    for outcome in ("failed", "refused", "failed"):
        m.finish_run(m.start_run("submit"), outcome)
    assert not limits.after_run(m, cfg)


# --- validation ---
def v(msg, past=()):
    return validate_message(msg, token=TOKEN, past_posts=list(past))


def test_validate_ok():
    assert v("The conditioning number controls how fast gradient descent converges.") == []


@pytest.mark.parametrize("msg", [
    "x" * 1300,
    f"my token is {TOKEN}",
    "Authorization: Bearer abcdef123456",
    "mail me at someone@example.com",
    "see https://evil.example.com/x",
    "see www.evil.com",
    "go to evil.com/path",
    "https://canvas.mit.edu@evil.com/",
    "",
])
def test_validate_rejects(msg):
    assert v(msg)


def test_validate_similarity():
    assert v("Great question about eigenvectors and PCA.", ["Great question about eigenvectors and PCA!"])
    assert not v("Totally different content here.", ["Great question about eigenvectors and PCA!"])


def test_html_to_text():
    assert html_to_text("<p>a&amp;b</p><script>x()</script><p>c<br>d</p>") == "a&b\n\nc\nd"


def test_http_error_is_logged_without_secrets(mock, caplog):
    mock.get_failures = [403]
    c = client(mock)
    with caplog.at_level("ERROR", logger="agent.canvas"):
        with pytest.raises(HTTPFailure):
            c.get_self()
    text = caplog.text
    assert "method=GET" in text and "/api/v1/users/self" in text
    assert "user_agent=canvas-forum-agent/0.1" in text
    assert "status=403" in text and "boom" in text
    assert TOKEN not in text and "Authorization" not in text and "Bearer" not in text


def test_error_body_logged_is_truncated(mock, caplog):
    mock.get_failures = [403]
    with caplog.at_level("ERROR", logger="agent.canvas"):
        with pytest.raises(HTTPFailure):
            client(mock).get_self()
    assert "query=[]" in caplog.text


def test_html_to_text_drops_script_and_style_with_contents():
    html = ("<p>Hello</p><STYLE>p{color:red}</STYLE><p>world &lt;b&gt;</p>"
            "<script type='text/javascript'>\nvar a = '<p>x</p>';\n</script>"
            "<script>alert(1)</script>")
    out = html_to_text(html)
    assert out == "Hello\n\nworld <b>"
    assert "alert" not in out and "color" not in out and "var a" not in out
