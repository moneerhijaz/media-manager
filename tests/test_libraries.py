import threading
from types import SimpleNamespace

import pytest

from media_manager.config import LibraryConfig
from media_manager.libraries import LibraryHub, configured_libraries
from media_manager.utils import MediaManagerError
from media_manager.web import Dashboard


def hub_at(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    primary = Dashboard(LibraryConfig.init(first), "test-dashboard-token-long")
    return LibraryHub(primary, [second]), second


def test_registration_allowlist_and_restart(tmp_path):
    hub, second = hub_at(tmp_path)
    assert len(hub.children) == 1
    with pytest.raises(MediaManagerError, match="configured"):
        hub.add({"library_id": "/etc"})
    key = hub.key(second)
    hub.add({"library_id": key})
    assert hub.target(key).cfg.root == second
    assert hub.target(key).cfg.state_dir != hub.primary.cfg.state_dir
    assert len(LibraryHub(hub.primary, [second]).children) == 2


def test_overlaps_and_symlinks_are_rejected(tmp_path):
    hub, _ = hub_at(tmp_path)
    nested = hub.primary.cfg.root / "nested"
    nested.mkdir()
    with pytest.raises(MediaManagerError, match="overlap"):
        LibraryHub(hub.primary, [nested])
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)
    with pytest.raises(MediaManagerError, match="overlap"):
        LibraryHub(hub.primary, [alias])


def test_combined_status_keeps_colliding_media_ids_separate(tmp_path, monkeypatch):
    hub, second = hub_at(tmp_path)
    hub.add({"library_id": hub.key(second)})
    for child in hub.children.values():
        monkeypatch.setattr(child, "status", lambda: {
            "counts": {"managed": 2}, "reviews": [{"media_id": 1, "path": "same.mp4"}],
            "runs": [], "active_run": None, "current_stage": None})
    data = hub.status("all")
    assert data["counts"] == {"managed": 4}
    assert len({row["library_id"] for row in data["reviews"]}) == 2


def test_queue_is_sequential_and_pause_cancels_pending(tmp_path, monkeypatch):
    hub, second = hub_at(tmp_path)
    hub.add({"library_id": hub.key(second)})
    release = threading.Event()
    started = []
    for key, child in hub.children.items():
        monkeypatch.setattr(child, "_active_run", lambda: False)
        def start(payload, key=key, child=child):
            started.append(key)
            child._child = SimpleNamespace(wait=lambda: release.wait(2))
            return {"pid": 123}
        monkeypatch.setattr(child, "start", start)
        monkeypatch.setattr(child, "pause", lambda: release.set() or {"message": "paused"})
    hub.start({"mode": "full"}, "all")
    assert len(started) == 1
    with pytest.raises(MediaManagerError, match="processing"):
        hub.start({"mode": "full"})
    hub.pause("all")
    hub.worker.join(2)
    assert len(started) == 1
    assert not hub.pending and hub.current is None


def test_queue_advances_after_exit(tmp_path, monkeypatch):
    hub, second = hub_at(tmp_path)
    hub.add({"library_id": hub.key(second)})
    calls = []
    for key, child in hub.children.items():
        monkeypatch.setattr(child, "_active_run", lambda: False)
        def start(payload, key=key, child=child):
            calls.append(key)
            child._child = SimpleNamespace(wait=lambda: 0)
            return {"pid": 1}
        monkeypatch.setattr(child, "start", start)
    hub.start({"mode": "full"}, "all")
    hub.worker.join(2)
    assert calls == list(hub.children)


@pytest.mark.parametrize("value", ['{}', '["relative"]', 'invalid'])
def test_invalid_library_configuration(monkeypatch, value):
    monkeypatch.setenv("MEDIA_LIBRARIES", value)
    with pytest.raises(MediaManagerError):
        configured_libraries()


def test_http_library_routes_require_auth_and_keep_settings_isolated(tmp_path):
    import json
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from media_manager.web import _handler
    hub, second = hub_at(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(hub.primary, hub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def request(path, payload=None, authorized=True):
        headers = {"Authorization": "Bearer " + hub.primary.token} if authorized else {}
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(base + path, headers=headers, data=data), timeout=3) as response:
            return json.load(response)
    try:
        with pytest.raises(HTTPError) as error:
            request("/api/libraries", authorized=False)
        assert error.value.code == 401
        key = hub.key(second)
        request("/api/libraries", {"library_id": key})
        data = request("/api/status?library=all")
        assert len(data["queue"]["libraries"]) == 2
        request("/api/settings?library=" + key, {"movies_directory": "Films"})
        assert request("/api/settings?library=" + key)["movies_directory"] == "Films"
        assert request("/api/settings")["movies_directory"] == "Movies"
        with pytest.raises(HTTPError):
            request("/api/settings?library=not-registered")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_standalone_empty_dashboard_has_no_fake_library(tmp_path):
    app = Dashboard(LibraryConfig.init(tmp_path / "app"), "test-dashboard-token-long")
    hub = LibraryHub(app, [], standalone=True)
    assert hub.status()["counts"] == {}
    assert hub.listing()["libraries"] == []
    assert hub.registry.exists()
    with pytest.raises(MediaManagerError, match="Add a media library"):
        hub.start({"mode": "full"})
    assert hub.pause()["message"] == "No active run; queue cleared"


def test_standalone_registry_persists_and_app_defaults_apply(tmp_path):
    app = Dashboard(LibraryConfig.init(tmp_path / "app"), "test-dashboard-token-long")
    app.cfg["library"]["movies_directory"] = "Films"
    root = tmp_path / "media"
    root.mkdir()
    hub = LibraryHub(app, [root], standalone=True)
    assert not hub.children
    hub.add({"library_id": hub.key(root)})
    assert hub.target(hub.key(root)).cfg["library"]["movies_directory"] == "Films"
    assert app.cfg.root not in [c.cfg.root for c in hub.children.values()]
    restarted = LibraryHub(app, [root], standalone=True)
    assert list(restarted.children) == [hub.key(root)]
    assert restarted.target(None) is app  # shared settings, not a media target
    assert restarted.status()["library"] == "All libraries"


def test_standalone_imports_existing_library_without_resetting_state(tmp_path):
    app = Dashboard(LibraryConfig.init(tmp_path / "app"), "test-dashboard-token-long")
    existing = LibraryConfig.init(tmp_path / "existing")
    existing["library"]["movies_directory"] = "KeepMe"
    existing.save()
    sentinel = existing.state_dir / "existing-checkpoint"
    sentinel.write_text("keep")
    hub = LibraryHub(app, [existing.root], standalone=True)
    assert hub.target(hub.key(existing.root)).cfg["library"]["movies_directory"] == "KeepMe"
    assert sentinel.read_text() == "keep"


def test_standalone_rejects_media_inside_app_state(tmp_path):
    app = Dashboard(LibraryConfig.init(tmp_path / "app"), "test-dashboard-token-long")
    with pytest.raises(MediaManagerError, match="separate"):
        LibraryHub(app, [app.cfg.root], standalone=True)


def test_cli_web_app_state_does_not_initialize_cwd_as_library(tmp_path, monkeypatch):
    from media_manager.cli import cmd_web
    from media_manager import web
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(web, "serve", lambda cfg, **kwargs: calls.append((cfg, kwargs)))
    cmd_web(SimpleNamespace(app_state=str(tmp_path / "state"), host="127.0.0.1", port=8787, token_file=None))
    assert calls[0][1]["standalone"] is True
    assert not LibraryConfig.exists(tmp_path)
