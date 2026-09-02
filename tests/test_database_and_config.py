from pathlib import Path

from media_manager.config import LibraryConfig
from media_manager.database import Database


def test_config_is_local_to_current_library_root(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path, os_user="user", os_api_key="key")
    assert cfg.state_dir == tmp_path / ".media-manager"
    assert cfg["library"]["root"] == str(tmp_path.resolve())
    assert cfg.path.exists()
    assert oct(cfg.path.stat().st_mode & 0o777) == "0o600"


def test_default_crop_baseline_samples_every_five_seconds(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    assert cfg["crop"]["scan_fps"] == 0.2


def test_default_avsync_profile_is_adaptive_and_cpu_bounded(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)["av_sync"]
    assert cfg["candidate_count"] == 8
    assert cfg["candidate_max_count"] == 64
    assert cfg["face_candidate_count"] == 48
    assert cfg["face_score_fps"] == 2.0
    assert cfg["syncnet_preprocess_max_width"] == 1280


def test_opensubtitles_password_is_stored_separately_with_private_permissions(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path, os_user="user", os_api_key="key")
    cfg.set_opensubtitles_password("secret password")

    assert cfg.get_opensubtitles_password() == "secret password"
    assert oct(cfg.opensubtitles_password_path.stat().st_mode & 0o777) == "0o600"
    assert "secret password" not in cfg.path.read_text(encoding="utf-8")


def test_database_tracks_present_vs_historical(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    with Database(cfg.state_dir / "media-manager.db") as db:
        assert db.rows() == []
        db.begin_scan()
        assert db.rows("present=0") == []
