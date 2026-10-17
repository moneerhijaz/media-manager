from datetime import datetime, timedelta, timezone

import pytest

from media_manager.avsync.engine import evaluate_saved_observations
from media_manager.config import DEFAULT_CONFIG
from media_manager.models import StageStatus, VisualType
from media_manager.retry_policy import eligible, waiting_details


@pytest.mark.parametrize("rate", [-201, -200, -120, 120, 200, 201])
def test_drift_boundaries(rate):
    data = {"observations": [
        {"time": t, "offset_frames": rate * t / 3600 / 40, "confidence": 8}
        for t in (0, 1800, 3600, 5400, 7200)
    ]}
    result = evaluate_saved_observations(data, DEFAULT_CONFIG["av_sync"], duration=7200, visual_type=VisualType.LIVE_ACTION)
    assert result.status == (StageStatus.NEEDS_REVIEW if abs(rate) > 200 else StageStatus.NOT_NEEDED)
    assert result.diagnostics["estimated_accumulated_drift_ms"] == pytest.approx(rate * 2)
    assert result.diagnostics.get("drift_warning", False) == (abs(rate) <= 200)


def test_retry_boundary_and_bounded_history():
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    details = waiting_details({"attempt_history": [{}] * 50}, {"reason": "bad candidate"}, DEFAULT_CONFIG["subtitles"], now=now)
    assert len(details["attempt_history"]) == 20
    assert not eligible(details, now + timedelta(days=6))
    assert eligible(details, now + timedelta(days=7))
    provider = waiting_details({}, {"reason": "quota"}, DEFAULT_CONFIG["subtitles"], provider=True, now=now)
    assert eligible(provider, now + timedelta(days=1))


def test_metadata_animation_requires_visual_corroboration():
    from media_manager.movie_metadata import reconcile_visual
    metadata = {"verified": True, "animation_genre": True}
    assert reconcile_visual(VisualType.LIVE_ACTION, 0.9, metadata)[0] == VisualType.MIXED
    assert reconcile_visual(VisualType.UNKNOWN, 0.5, metadata)[0] == VisualType.UNKNOWN
    assert reconcile_visual(VisualType.ANIMATION, 0.9, metadata)[0] == VisualType.ANIMATION


def test_language_sampling_and_dub_selection():
    from media_manager.audio_languages import summarize, selections, inventory
    from types import SimpleNamespace
    samples = [{"language": "ja", "probability": .99, "speech": True}] * 3
    result = summarize(samples, "en")
    assert result["tag_conflict"] and result["detected_language"] == "ja"
    assert summarize(samples + [{"language": "en", "probability": .99, "speech": True}], "ja")["status"] == "multilingual"
    assert summarize([{**samples[0], "speech": False}], "ja")["status"] == "uncertain"
    tracks = inventory(SimpleNamespace(audio_streams=[
        {"index": 1, "tags": {"language": "eng", "title": "Commentary"}},
        {"index": 2, "tags": {"language": "eng", "title": "English dub"}},
        {"index": 3, "tags": {"language": "jpn"}},
    ]))
    tracks[1].update(status="verified", detected_language="en")
    tracks[2].update(status="verified", detected_language="ja")
    selection = selections({"tracks": tracks}, {"verified": True, "original_language": "ja"}, ["en"])
    assert selection["playback_audio_index"] == 1
    assert selection["av_reference_audio_index"] == 2
    assert selections({"tracks": tracks[:2]}, {"verified": True, "original_language": "ja"}, ["en"])["av_reference_audio_index"] is None


def test_release_boundary_extends_cooldown():
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    details = waiting_details({}, {"release_eligible_date": "2026-12-01"}, DEFAULT_CONFIG["subtitles"], now=now)
    assert not eligible(details, datetime(2026, 11, 30, tzinfo=timezone.utc))
    assert eligible(details, datetime(2026, 12, 1, tzinfo=timezone.utc))


def test_successful_stages_preserved_across_policy_upgrade():
    from media_manager.executor import _stage_needs_work
    for name in ("av_sync", "subtitle:en"):
        assert not _stage_needs_work(name, {"status": "complete", "details_json": '{"algorithm_version":"old"}'})


def test_waiting_eligibility_survives_database_restart(tmp_path):
    import json
    from media_manager.database import Database
    from media_manager.executor import _stage_needs_work
    path = tmp_path / "test.db"
    details = waiting_details({}, {"reason": "no candidate"}, DEFAULT_CONFIG["subtitles"])
    with Database(path) as db:
        media_id = int(db.conn.execute("INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,last_seen) VALUES('movie.mp4','movie','Movie',4,1,'fp',100,'{}','now')").lastrowid)
        db.set_stage(media_id, "subtitle:en", StageStatus.WAITING, details)
    with Database(path) as db:
        stage = db.get_stage(media_id, "subtitle:en")
        assert not _stage_needs_work("subtitle:en", stage)
        assert json.loads(stage["details_json"])["attempt_history"] == details["attempt_history"]


def test_subtitle_quota_does_not_abort_remaining_stages(tmp_path, monkeypatch):
    from media_manager import executor
    from media_manager.config import LibraryConfig
    from media_manager.database import Database
    from media_manager.subtitles import QuotaExhausted
    cfg = LibraryConfig.init(tmp_path)
    cfg["subtitles"]["languages"] = ["en", "ar"]
    def unavailable(*args, **kwargs):
        raise QuotaExhausted("quota")
    monkeypatch.setattr(executor, "process_subtitle_language", unavailable)
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = int(db.conn.execute("INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,last_seen) VALUES('movie.mp4','movie','Movie',4,1,'fp',100,'{}','now')").lastrowid)
        executor._subtitle_stages(tmp_path / "movie.mp4", db.get_media(media_id), cfg, db, tmp_path, None)
        assert db.get_stage(media_id, "subtitle:en")["status"] == "waiting"
        assert db.get_stage(media_id, "subtitle:ar")["status"] == "waiting"


def test_metadata_rejects_ambiguous_title_matches(monkeypatch):
    import json
    from media_manager import movie_metadata
    from io import BytesIO
    monkeypatch.setenv("TMDB_READ_TOKEN", "test-token-not-a-secret")
    matches = [{"id": i, "title": "Example", "release_date": "2020-01-01"} for i in (1, 2)]
    monkeypatch.setattr(movie_metadata, "urlopen", lambda *a, **k: BytesIO(json.dumps({"results": matches}).encode()))
    result = movie_metadata.lookup({"media_type": "movie", "rel_path": "Example (2020).mp4", "title": "Example", "year": 2020})
    assert not result["verified"]


def test_digital_release_is_regional_and_not_theatrical(monkeypatch):
    import json
    from media_manager import movie_metadata
    from io import BytesIO
    monkeypatch.setenv("TMDB_READ_TOKEN", "test-token-not-a-secret")
    movie = {"genres": [{"id": 16}], "original_language": "ja", "release_dates": {"results": [
        {"iso_3166_1": "US", "release_dates": [{"type": 3, "release_date": "2026-01-01"}, {"type": 4, "release_date": "2026-03-01"}]},
        {"iso_3166_1": "JP", "release_dates": [{"type": 4, "release_date": "2026-02-01"}]},
    ]}}
    monkeypatch.setattr(movie_metadata, "urlopen", lambda *a, **k: BytesIO(json.dumps(movie).encode()))
    row = {"media_type": "movie", "rel_path": "Example {tmdb-123}.mp4"}
    assert movie_metadata.lookup(row)["subtitle_eligible_date"] == "2026-03-08"
    movie["release_dates"]["results"][0]["release_dates"].append({"type": 4, "release_date": "2026-04-01"})
    result = movie_metadata.lookup(row)
    assert result["release_conflict"] and result["subtitle_eligible_date"] is None


def test_subtitle_journal_does_not_mistake_original_for_replacement(tmp_path, monkeypatch):
    import hashlib
    from media_manager import executor
    from media_manager.checkpoints import CheckpointStore
    from media_manager.config import LibraryConfig
    from media_manager.database import Database
    cfg = LibraryConfig.init(tmp_path)
    final = tmp_path / "movie.en.srt"
    final.write_text("original")
    replacement = tmp_path / "replacement.srt"
    replacement.write_text("validated replacement")
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = int(db.conn.execute("INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,last_seen) VALUES('movie.mp4','movie','Movie',4,1,'fp',100,'{}','now')").lastrowid)
        CheckpointStore(cfg.state_dir, media_id).save("subtitle:en", {
            "phase": "ready_to_commit", "commit_temp": str(replacement),
            "output_sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
            "final_stage_details": {"algorithm_version": executor.SUBTITLE_ALGORITHM_VERSION},
        })
        monkeypatch.setattr(executor, "process_subtitle_language", lambda *a, **k: pytest.fail("Must recover journal"))
        executor._subtitle_stages(tmp_path / "movie.mp4", db.get_media(media_id), cfg, db, tmp_path, None)
        assert final.read_text() == "validated replacement"
        assert db.get_stage(media_id, "subtitle:en")["status"] == "complete"
