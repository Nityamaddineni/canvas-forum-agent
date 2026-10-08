import io
import json

import pytest

from agent import cli, config
from tests.mock_canvas import BASE, COURSE, TOKEN, TOPIC, MockCanvas  # noqa: F401


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Recovery waits are recorded, not slept."""
    calls = []
    monkeypatch.setattr("agent.pipeline.time.sleep", calls.append)
    return calls


@pytest.fixture
def mock():
    m = MockCanvas()
    yield m
    m.stop()


@pytest.fixture
def env(mock, tmp_path):
    return {"COURSE_ID": COURSE, "TOPIC_ID": TOPIC, "BASE_URL": mock.url,
            "CANVAS_TOKEN": TOKEN, "STATE_DIR": str(tmp_path / "state")}


class Runner:
    def __init__(self, env, capsys):
        self.env, self.capsys, self.now = env, capsys, 1_000_000.0

    def run(self, *argv, stdin="", **env_over):
        out = io.StringIO()
        code = cli.main(list(argv), env={**self.env, **env_over}, stdin=io.StringIO(stdin),
                        stdout=out, clock=lambda: self.now)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None)

    def run_text(self, *argv, **env_over):
        out = io.StringIO()
        code = cli.main(list(argv), env={**self.env, **env_over}, stdin=io.StringIO(""),
                        stdout=out, clock=lambda: self.now)
        return code, out.getvalue()

    def submit(self, decision, **env_over):
        return self.run("submit", stdin=json.dumps(decision), **env_over)


@pytest.fixture
def runner(env, capsys):
    config._SECRETS.clear()
    return Runner(env, capsys)
