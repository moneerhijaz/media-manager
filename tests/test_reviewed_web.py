from __future__ import annotations

import json
from pathlib import Path

from media_manager.cli import _reviewed_selection, build_parser
from media_manager.config import LibraryConfig
from media_manager.database import Database
from media_manager.models import StageStatus
from media_manager.web import Dashboard, load_or_create_token
from media_manager.executor import _stage_selected


def test_dashboard_detects_host_run_using_shared_lock(tmp_path, monkeypatch):
    import pytest
    from media_manager.cli import _library_lock
    from media_manager.utils import MediaManagerError

    cfg = LibraryConfig.init(tmp_path)
    dashboard = Dashboard(cfg, "long-enough-dashboard-token")
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = _media(db, "Movies/Example.mp4")
        db.start_run(pid=99999999, hostname="external-host", command="media-manager run")
        db.set_stage(media_id, "av_sync", StageStatus.RUNNING, {"phase": "syncnet"})
    monkeypatch.setattr("media_manager.web._pid_alive", lambda pid: False)
    with _library_lock(cfg):
        status = dashboard.status()
        assert status["active_run"]["pid"] == 99999999
        assert status["active_run"]["can_pause"] is False
        assert status["current_stage"]["phase"] == "syncnet"
        with pytest.raises(MediaManagerError, match="busy"):
            dashboard.start({"mode": "full"})
        with pytest.raises(MediaManagerError, match="Pause the active"):
            dashboard.update_settings({})
        with pytest.raises(MediaManagerError, match="host terminal"):
            dashboard.pause()
    # A stale database row must not keep the dashboard running after exit.
    assert dashboard.status()["active_run"] is None
    assert dashboard.status()["current_stage"] is None


def _media(db: Database, path: str, status: str = "needs_review") -> int:
    return int(db.conn.execute(
        """INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,
           probe_json,overall_status,last_seen,present)
           VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
        (path, "movie", "Example", 1, 1, "fp-" + path, 3600.0, "{}", status, "now"),
    ).lastrowid)


def test_prepare_reviewed_retry_preserves_evidence_and_successful_stages(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = _media(db, "Movies/Example (2024)/Example (2024).mp4")
        evidence = {"algorithm_version": "avsync-v8-syncnet-adapter-fix", "observations": [{"time": 1}]}
        db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, evidence)
        db.set_stage(media_id, "crop", StageStatus.COMPLETE, {"algorithm_version": "crop-current"})

        selected, count = db.prepare_reviewed_retry(media_ids=[media_id])

        assert count == 1
        assert selected == {media_id: ["av_sync"]}
        av = db.get_stage(media_id, "av_sync")
        assert av["status"] == "pending"
        assert json.loads(av["details_json"])["previous_review"] == evidence
        assert db.get_stage(media_id, "crop")["status"] == "complete"


def test_review_selection_limit_counts_media_not_stages(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    with Database(cfg.state_dir / "media-manager.db") as db:
        first = _media(db, "Movies/A (2024)/A (2024).mp4")
        second = _media(db, "Movies/B (2024)/B (2024).mp4")
        db.set_stage(first, "av_sync", StageStatus.NEEDS_REVIEW, {})
        db.set_stage(first, "subtitle:en", StageStatus.INSUFFICIENT_EVIDENCE, {})
        db.set_stage(second, "av_sync", StageStatus.NEEDS_REVIEW, {})
        assert _reviewed_selection(db, limit=1) == {first: ["av_sync", "subtitle:en"]}


def test_review_selection_can_target_one_media_and_includes_failed_stages(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    with Database(cfg.state_dir / "media-manager.db") as db:
        first = _media(db, "Movies/A (2024)/A (2024).mp4", "failed")
        second = _media(db, "Movies/B (2024)/B (2024).mp4")
        db.set_stage(first, "subtitle:en", StageStatus.FAILED, {"error": "test failure"})
        db.set_stage(second, "av_sync", StageStatus.NEEDS_REVIEW, {})
        assert _reviewed_selection(db, media_id=first) == {first: ["subtitle:en"]}
        selected, count = db.prepare_reviewed_retry(media_ids=[first])
        assert count == 1
        assert selected == {first: ["subtitle:en"]}
        assert db.get_stage(first, "subtitle:en")["status"] == "pending"


def test_cli_exposes_reviewed_and_web_commands():
    parser = build_parser()
    args = parser.parse_args(["rerun-reviewed", "--review-stage", "av_sync", "--limit", "2"])
    assert args.reviewed_only is True
    assert args.review_stage == "av_sync"
    single = parser.parse_args(["run", "--reviewed-only", "--media-id", "7"])
    assert single.media_id == 7
    web = parser.parse_args(["web", "--port", "9000"])
    assert web.port == 9000
    assert web.host == "0.0.0.0"


def test_reviewed_av_retry_allows_only_required_downstream_stages(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    cfg.data["_runtime"] = {"selected_stages": {"7": ["av_sync"]}}
    assert _stage_selected(cfg, 7, "av_sync")
    assert _stage_selected(cfg, 7, "subtitle:en")
    assert _stage_selected(cfg, 7, "final_verify")
    assert not _stage_selected(cfg, 7, "crop")
    assert not _stage_selected(cfg, 8, "av_sync")


def test_dashboard_browser_setup_creates_private_token_and_status_contains_review_details(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    token, path, setup_required = load_or_create_token(cfg)
    assert setup_required and path is not None and token == ""
    dashboard = Dashboard(cfg, token, token_path=path, setup_required=setup_required)
    chosen = "a-secure-dashboard-token"
    dashboard.update_settings({"dashboard_token": chosen}, initial=True)
    token = chosen
    assert path.stat().st_mode & 0o777 == 0o600
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = _media(db, "Movies/Example (2024)/Example (2024).mp4")
        db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, {"reason": "Review example"})
    assert dashboard.authorized("Bearer " + token)
    assert not dashboard.authorized("Bearer wrong")
    status = dashboard.status()
    assert status["counts"] == {"needs_review": 1}
    assert status["reviews"][0]["reason"] == "Review example"


def test_dashboard_settings_are_redacted_and_existing_token_skips_setup(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    dashboard = Dashboard(cfg, "", token_path=cfg.state_dir / "web-token", setup_required=True)
    dashboard.update_settings({
        "dashboard_token": "long-enough-dashboard-token",
        "subtitle_languages": "en,es",
        "opensubtitles_username": "account",
        "opensubtitles_api_key": "private-key",
        "opensubtitles_password": "private-password",
        "syncnet_device": "cuda",
    }, initial=True)
    settings = dashboard.settings()
    assert settings["subtitle_languages"] == ["en", "es"]
    assert settings["syncnet_device"] == "cuda"
    assert settings["opensubtitles_api_key_configured"] is True
    assert settings["opensubtitles_password_configured"] is True
    assert "private-key" not in json.dumps(settings)
    assert "private-password" not in json.dumps(settings)
    token, _, setup_required = load_or_create_token(cfg)
    assert token == "long-enough-dashboard-token"
    assert setup_required is False


def test_dashboard_single_item_run_builds_scoped_command(tmp_path: Path, monkeypatch):
    cfg = LibraryConfig.init(tmp_path)
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = _media(db, "Movies/Example (2024)/Example (2024).mp4")
        db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, {})
    captured = {}

    class Child:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Child()

    monkeypatch.setattr("media_manager.web.subprocess.Popen", fake_popen)
    dashboard = Dashboard(cfg, "long-enough-dashboard-token")
    result = dashboard.start({"mode": "single", "media_id": media_id})
    assert result["mode"] == "single"
    assert "--reviewed-only" in captured["command"]
    assert captured["command"][-2:] == ["--media-id", str(media_id)]
