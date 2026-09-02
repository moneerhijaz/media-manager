from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_manager.checkpoints import CheckpointStore
from media_manager.config import LibraryConfig
from media_manager.crop import engine as crop_engine
from media_manager.database import Database
from media_manager.models import CropSample, MediaIdentity, MediaType, ProbeInfo, StageStatus


def _probe(duration: float = 130.0) -> ProbeInfo:
    return ProbeInfo(
        duration=duration,
        format_name="mp4",
        streams=[],
        chapters=[],
        video_codec="h264",
        width=640,
        height=360,
        audio_streams=[],
        subtitle_streams=[],
        raw={},
    )


def test_database_recovers_running_stages_and_runs(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path / "library")
    with Database(cfg.state_dir / "media-manager.db") as db:
        # Minimal media row is enough for stage recovery.
        media_id = db.conn.execute(
            """INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,overall_status,last_seen)
               VALUES('Movies/X/X.mp4','movie','X',1,1,'fp',1.0,'{}','incomplete','now')"""
        ).lastrowid
        db.set_stage(int(media_id), "crop", StageStatus.RUNNING, {"phase": "baseline"})
        run_id = db.start_run(pid=12345, hostname="host", command="media-manager run")
        recovered = db.recover_interrupted_state()
        assert recovered["stages"] == 1
        assert recovered["runs"] == 1
        assert db.get_stage(int(media_id), "crop")["status"] == StageStatus.PENDING.value
        assert db.conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"] == "interrupted"


def test_crop_resumes_completed_chunks(tmp_path: Path, monkeypatch):
    root = tmp_path / "library"
    cfg = LibraryConfig.init(root)
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"x" * 1024)
    store = CheckpointStore(cfg.state_dir, 7)
    calls: list[float] = []
    first_pass = {"interrupt": True}

    def fake_scan(path, *, source_width, source_height, fps, limit=None, round_value=None, start=None, duration=None, **kwargs):
        start = float(start or 0.0)
        calls.append(start)
        if first_pass["interrupt"] and start >= 60.0:
            raise KeyboardInterrupt
        return [CropSample(start, 0, 0, 0, 0, 100.0, 120.0, True)], source_width, source_height

    monkeypatch.setattr(crop_engine, "_scan", fake_scan)
    crop_cfg = dict(cfg["crop"])
    crop_cfg["checkpoint_seconds"] = 60.0

    with pytest.raises(KeyboardInterrupt):
        crop_engine.analyze_crop(src, _probe(), crop_cfg, checkpoint_store=store)

    cp = store.load("crop")
    assert "0" in cp["baseline_chunks"]
    assert "1" not in cp["baseline_chunks"]
    assert calls.count(0.0) == 1

    first_pass["interrupt"] = False
    result = crop_engine.analyze_crop(src, _probe(), crop_cfg, checkpoint_store=store)
    assert result.status == StageStatus.NOT_NEEDED
    # Chunk 0 was restored from JSON rather than rescanned.
    assert calls.count(0.0) == 1
    assert 60.0 in calls and 120.0 in calls


def test_pause_waits_for_exit_and_recovers_last_checkpoint(tmp_path: Path, monkeypatch):
    import socket
    import subprocess

    from media_manager.cli import cmd_pause

    root = tmp_path / "library"
    cfg = LibraryConfig.init(root)
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = int(db.conn.execute(
            """INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,overall_status,last_seen)
               VALUES('Movies/X/X.mp4','movie','X',1,1,'fp',1.0,'{}','incomplete','now')"""
        ).lastrowid)
        db.set_stage(media_id, "av_sync", StageStatus.RUNNING, {"phase": "syncnet"})

    proc = subprocess.Popen(
        ["bash", "-c", "exec -a media-manager python3 -c 'import time; time.sleep(60)'"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with Database(cfg.state_dir / "media-manager.db") as db:
            db.start_run(pid=proc.pid, hostname=socket.gethostname(), command="media-manager resume")
        monkeypatch.chdir(root)
        assert cmd_pause(SimpleNamespace(timeout=5.0, force=False)) == 0
        proc.wait(timeout=5)
        with Database(cfg.state_dir / "media-manager.db") as db:
            assert db.latest_run()["status"] == "interrupted"
            assert db.get_stage(media_id, "av_sync")["status"] == StageStatus.PENDING.value
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
