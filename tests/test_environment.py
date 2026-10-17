import json

import pytest

from media_manager import environment
from media_manager.utils import MediaManagerError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    original = {name: environment.os.environ.get(name) for name in environment.SUPPORTED}
    for name in environment.SUPPORTED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(environment.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(environment, "__file__", str(tmp_path / "package" / "media_manager" / "environment.py"))
    monkeypatch.setattr(environment, "installed_source", lambda: tmp_path / "project")
    (tmp_path / "project").mkdir()
    yield tmp_path
    for name, value in original.items():
        if value is None:
            environment.os.environ.pop(name, None)
        else:
            environment.os.environ[name] = value


def test_load_project_env_when_launched_from_library(isolated):
    (isolated / "project" / ".env").write_text("TMDB_READ_TOKEN='example-token'\n")
    environment.load_environment(isolated / "library")
    assert environment.os.environ["TMDB_READ_TOKEN"] == "example-token"


def test_exported_environment_takes_precedence(isolated, monkeypatch):
    monkeypatch.setenv("TMDB_READ_TOKEN", "exported")
    (isolated / "project" / ".env").write_text("TMDB_READ_TOKEN=local\n")
    environment.load_environment(isolated)
    assert environment.os.environ["TMDB_READ_TOKEN"] == "exported"


def test_no_shell_execution_or_unrelated_environment_changes(isolated, monkeypatch):
    monkeypatch.delenv("UNRELATED_TEST_SETTING", raising=False)
    (isolated / ".env").write_text("export TMDB_READ_TOKEN='$(touch should-not-exist)' # comment\nUNRELATED_TEST_SETTING=bad\n")
    environment.load_environment(isolated)
    assert environment.os.environ["TMDB_READ_TOKEN"] == "$(touch should-not-exist)"
    assert "UNRELATED_TEST_SETTING" not in environment.os.environ
    assert not (isolated / "should-not-exist").exists()


def test_bad_quote_error_does_not_disclose_secret(isolated):
    (isolated / ".env").write_text("TMDB_READ_TOKEN='private-value\n")
    with pytest.raises(MediaManagerError) as error:
        environment.load_environment(isolated)
    assert "private-value" not in str(error.value)


def test_pipx_local_install_source(monkeypatch, tmp_path):
    class Dist:
        def read_text(self, name):
            return json.dumps({"url": tmp_path.as_uri(), "dir_info": {}})
    monkeypatch.setattr(environment, "distribution", lambda name: Dist())
    assert environment.installed_source() == tmp_path


def test_opensubtitles_env_credentials_reach_client(isolated):
    from types import SimpleNamespace
    from media_manager.cli import _client_config
    from media_manager.subtitles.engine import OpenSubtitlesClient
    (isolated / ".env").write_text(
        "OPENSUBTITLES_USERNAME='test-user'\n"
        "OPENSUBTITLES_API_KEY='test-key'\n"
        "OPENSUBTITLES_PASSWORD='test password # quoted'\n"
    )
    environment.load_environment(isolated)
    cfg = _client_config({"opensubtitles": {}}, SimpleNamespace())
    client = OpenSubtitlesClient(cfg)
    assert client.configured()
    assert client.username == "test-user"
    assert client.api_key == "test-key"
    assert client.password == "test password # quoted"


def test_empty_compose_password_preserves_stored_password(monkeypatch):
    from media_manager.subtitles.engine import OpenSubtitlesClient
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", "")
    assert OpenSubtitlesClient({}, password="stored").password == "stored"
