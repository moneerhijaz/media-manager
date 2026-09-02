import io
from types import SimpleNamespace

from media_manager import executor
from media_manager.cli import _redacted_command
from media_manager.progress import ProgressDisplay


def test_redacts_opensubtitles_api_key_from_run_history():
    cmd = _redacted_command(["media-manager", "run", "--os-api-key", "SECRET", "--limit", "1"])
    assert "SECRET" not in cmd
    assert "<redacted>" in cmd


def test_secret_prompt_suspends_live_renderer(monkeypatch):
    stream = io.StringIO()
    ui = ProgressDisplay(enabled=True, stream=stream)
    observed = {}

    def fake_getpass(prompt, stream=None):
        observed["prompt"] = prompt
        observed["stream"] = stream
        observed["suspended"] = ui._suspended.is_set()
        return "secret"

    monkeypatch.setattr("media_manager.progress.getpass.getpass", fake_getpass)
    try:
        assert ui.prompt_secret("OpenSubtitles password: ") == "secret"
    finally:
        ui.close()

    assert observed == {
        "prompt": "OpenSubtitles password: ",
        "stream": stream,
        "suspended": True,
    }
    assert not ui._suspended.is_set()


def test_batch_routes_lazy_password_prompt_through_progress(monkeypatch):
    class FakeProgress:
        def __init__(self, **_kwargs):
            self.closed = False

        def prompt_secret(self, prompt):
            return prompt

        def scan(self, _message):
            pass

        def close(self):
            self.closed = True

    ui = FakeProgress()
    client = SimpleNamespace(password_prompt=None)
    db = SimpleNamespace(rows=lambda _where: [])
    monkeypatch.setattr(executor, "ProgressDisplay", lambda **_kwargs: ui)
    monkeypatch.setattr(executor, "scan_library", lambda *_args, **_kwargs: None)

    assert executor.scan_and_run(object(), db, client=client) == {}
    assert client.password_prompt == ui.prompt_secret
    assert ui.closed
