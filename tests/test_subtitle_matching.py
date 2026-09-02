from pathlib import Path
from types import SimpleNamespace

import pytest

from media_manager.models import StageStatus
from media_manager.subtitles import engine
from media_manager.subtitles.engine import choose_candidate
from media_manager.utils import MediaManagerError


def _result(*, title, feature_type, season=None, episode=None, parent=None, file_id=1, release="", hash_match=False):
    return {
        "attributes": {
            "language": "en",
            "release": release,
            "moviehash_match": hash_match,
            "from_trusted": True,
            "download_count": 100,
            "ratings": 8.0,
            "feature_details": {
                "feature_type": feature_type,
                "title": title,
                "season_number": season,
                "episode_number": episode,
                "parent_title": parent,
            },
            "files": [{"file_id": file_id, "file_name": f"{release}.srt"}],
        }
    }


def test_opensubtitles_login_uses_injected_password_prompt(monkeypatch):
    prompts = []
    requests = []
    client = engine.OpenSubtitlesClient(
        {"username": "user", "api_key": "key"},
        password_prompt=lambda prompt: prompts.append(prompt) or "secret",
    )

    def fake_request(method, endpoint, **kwargs):
        requests.append((method, endpoint, kwargs))
        return {"token": "token"}

    monkeypatch.setattr(engine.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(client, "_request", fake_request)

    client.login()

    assert prompts == ["OpenSubtitles password: "]
    assert requests[0][2]["body"] == {"username": "user", "password": "secret"}
    assert client.token == "token"


def test_environment_password_overrides_stored_password(monkeypatch):
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", "environment-secret")
    client = engine.OpenSubtitlesClient(
        {"username": "user", "api_key": "key"},
        password="stored-secret",
    )
    assert client.password == "environment-secret"


def test_episode_rejects_movie_result_that_only_looks_similar():
    row = {
        "media_type": "episode",
        "show_title": "Example Series",
        "title": "Example Series",
        "year": 2026,
        "season": 1,
        "episode": 1,
    }
    wrong = _result(
        title="Unrelated Feature",
        feature_type="Movie",
        release="UNRELATED FEATURE (2026) WEB",
    )
    assert choose_candidate([wrong], row, Path("Example Series (2025) - s01e01 - First Chapter.mp4")) is None


def test_episode_requires_exact_season_episode_and_show():
    row = {
        "media_type": "episode",
        "show_title": "Example Series",
        "title": "Example Series",
        "year": 2026,
        "season": 1,
        "episode": 1,
    }
    wrong_ep = _result(
        title="Episode 2", feature_type="Episode", season=1, episode=2,
        parent="Example Series", release="Example.Series.S01E02", file_id=2,
    )
    right = _result(
        title="First Chapter", feature_type="Episode", season=1, episode=1,
        parent="Example Series", release="Example.Series.S01E01.1080p.WEB", file_id=3,
    )
    choice = choose_candidate([wrong_ep, right], row, Path("Example Series (2025) - s01e01 - First Chapter.mp4"))
    assert choice is not None
    assert choice.file_id == 3


def test_pal_film_ratio_is_explained():
    from media_manager.subtitles.engine import _known_timing_conversion
    assert "23.976" in _known_timing_conversion(25.0 / (24000.0 / 1001.0), 0.003)
    assert "25" in _known_timing_conversion((24000.0 / 1001.0) / 25.0, 0.003)


def test_ffsubsync_low_quality_noop_returns_diagnostics_instead_of_failure(monkeypatch, tmp_path):
    source = tmp_path / "existing.en.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    output = tmp_path / "synced.en.srt"
    seen_command = []

    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="--multi-segment-sync --skip-sync-on-low-quality --min-score --quality-max-offset-seconds",
                stderr="",
            )
        seen_command.extend(command)
        output.write_bytes(source.read_bytes())
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=(
                "score: -324423.760\n"
                "offset seconds: 0.000\n"
                "framerate scale factor: 1.000\n"
                "low-quality alignment (score -324423.8 < 0.0); leaving subtitles unmodified\n"
            ),
        )

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine._run_ffsubsync(
        tmp_path / "movie.mp4",
        source,
        output,
        {
            "multi_segment_sync": True,
            "segment_count": 8,
            "ffsubsync_min_score": 0.0,
            "max_abs_offset_seconds": 600.0,
            "min_scale": 0.90,
            "max_scale": 1.10,
        },
        0,
    )

    assert result["score"] == -324423.76
    assert result["applied"] is False
    assert result["low_quality"] is True
    assert result["low_quality_reason"] == "score -324423.8 < 0.0"
    assert result["sync_applied"] is False
    assert result["quality_rejected"] is True
    assert result["quality_reasons"] == ["score -324423.8 < 0.0"]
    assert output.read_bytes() == source.read_bytes()
    assert "--skip-sync-on-low-quality" in seen_command
    assert "--min-score" not in seen_command
    assert "--quality-max-offset-seconds" not in seen_command


def test_ffsubsync_wrapped_low_quality_warning_is_recognized(monkeypatch, tmp_path):
    source = tmp_path / "existing.en.srt"
    source.write_text("subtitle", encoding="utf-8")
    output = tmp_path / "synced.en.srt"
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout="--skip-sync-on-low-quality", stderr="")
        output.write_bytes(source.read_bytes())
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=(
                "low-quality alignment (score -63575.7 <     ffsubsync.py:269\n"
                "                    0.0; |offset| 35.0s > 30.0s); leaving       \n"
                "                    subtitles unmodified\n"
                "score: -63575.720\noffset seconds: -35.050\n"
                "framerate scale factor: 1.042\n"
            ),
        )

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine._run_ffsubsync(
        tmp_path / "movie.mp4", source, output,
        {"ffsubsync_min_score": 0.0, "max_abs_offset_seconds": 600.0, "min_scale": 0.9, "max_scale": 1.1},
        0,
    )

    assert result["quality_rejected"] is True
    assert result["low_quality_reason"] == "score -63575.7 < 0.0; |offset| 35.0s > 30.0s"


def test_ffsubsync_low_quality_full_runtime_falls_back_to_multi_segment(monkeypatch, tmp_path):
    source = tmp_path / "existing.en.srt"
    source.write_text("subtitle", encoding="utf-8")
    output = tmp_path / "synced.en.srt"
    commands = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="--multi-segment-sync --skip-sync-on-low-quality",
                stderr="",
            )
        commands.append(command.copy())
        if "--multi-segment-sync" not in command:
            output.write_bytes(source.read_bytes())
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "score: -63575.720\noffset seconds: -35.050\nframerate scale factor: 1.042\n"
                    "low-quality alignment (score -63575.7 < 0.0); leaving subtitles unmodified\n"
                ),
                stderr="",
            )
        output.write_text("synchronized", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="score: 151568.0\noffset seconds: 0.010\nframerate scale factor: 1.000\n",
            stderr="",
        )

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine._run_ffsubsync(
        tmp_path / "movie.mp4", source, output,
        {
            "multi_segment_sync": True, "segment_count": 8,
            "ffsubsync_min_score": 0.0, "max_abs_offset_seconds": 600.0,
            "min_scale": 0.9, "max_scale": 1.1,
        },
        0,
    )

    assert len(commands) == 2
    assert "--multi-segment-sync" not in commands[0]
    assert "--multi-segment-sync" in commands[1]
    assert result["method"] == "multi_segment_fallback"
    assert result["multi_segment_fallback_attempted"] is True
    assert result["quality_rejected"] is False
    assert result["score"] == 151568.0
    assert result["offset_seconds"] == 0.01
    assert result["full_runtime_attempt"]["quality_rejected"] is True
    assert output.read_text(encoding="utf-8") == "synchronized"


def test_ffsubsync_uses_full_runtime_without_multi_segment_when_primary_is_good(monkeypatch, tmp_path):
    source = tmp_path / "existing.en.srt"
    source.write_text("subtitle", encoding="utf-8")
    output = tmp_path / "synced.en.srt"
    commands = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout="--multi-segment-sync", stderr="")
        commands.append(command.copy())
        output.write_text("synchronized", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="score: 100.0\noffset seconds: 0.0\nframerate scale factor: 1.0\n",
            stderr="",
        )

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine._run_ffsubsync(
        tmp_path / "movie.mp4", source, output,
        {"multi_segment_sync": True, "segment_count": 8, "ffsubsync_min_score": 0.0,
         "max_abs_offset_seconds": 600.0, "min_scale": 0.9, "max_scale": 1.1},
        0,
    )

    assert len(commands) == 1
    assert "--multi-segment-sync" not in commands[0]
    assert result["method"] == "full_runtime"
    assert result["multi_segment_fallback_attempted"] is False


def test_runtime_invalid_full_result_falls_back_to_multi_segment(monkeypatch, tmp_path):
    source = tmp_path / "existing.en.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nOne\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nThree\n",
        encoding="utf-8",
    )
    output = tmp_path / "synced.en.srt"
    commands = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout="--multi-segment-sync", stderr="")
        commands.append(command.copy())
        if "--multi-segment-sync" not in command:
            output.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nOne\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n"
                "3\n00:04:59,000 --> 00:05:00,000\nThree\n",
                encoding="utf-8",
            )
            return SimpleNamespace(
                returncode=0,
                stdout="score: 200.0\noffset seconds: 0.0\nframerate scale factor: 1.042709\n",
                stderr="",
            )
        output.write_bytes(source.read_bytes())
        return SimpleNamespace(
            returncode=0,
            stdout="score: 150.0\noffset seconds: 0.0\nframerate scale factor: 1.0\n",
            stderr="",
        )

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine._run_ffsubsync(
        tmp_path / "movie.mp4", source, output,
        {"multi_segment_sync": True, "segment_count": 8, "ffsubsync_min_score": 0.0,
         "max_abs_offset_seconds": 600.0, "min_scale": 0.9, "max_scale": 1.1},
        0,
        candidate_validator=lambda path: engine._validate_srt(path, 100.0),
    )

    assert len(commands) == 2
    assert "--multi-segment-sync" not in commands[0]
    assert "--multi-segment-sync" in commands[1]
    assert result["method"] == "multi_segment_fallback"
    assert "beyond media duration" in result["multi_segment_fallback_reason"]
    assert result["candidate_validation"]["valid"] is True


def test_ffsubsync_negative_score_without_safe_noop_still_fails(monkeypatch, tmp_path):
    source = tmp_path / "candidate.en.srt"
    source.write_text("subtitle", encoding="utf-8")
    output = tmp_path / "synced.en.srt"
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        output.write_bytes(source.read_bytes())
        return SimpleNamespace(
            returncode=0,
            stdout="score: -1.0\noffset seconds: 0.0\nframerate scale factor: 1.0\n",
            stderr="",
        )

    monkeypatch.setattr(engine, "run", fake_run)
    with pytest.raises(MediaManagerError, match="score -1.0 below configured minimum"):
        engine._run_ffsubsync(
            tmp_path / "movie.mp4", source, output,
            {"ffsubsync_min_score": 0.0, "max_abs_offset_seconds": 600.0, "min_scale": 0.9, "max_scale": 1.1},
            0,
        )


def test_ffsubsync_explicit_offset_quality_noop_is_not_a_hard_failure(monkeypatch, tmp_path):
    source = tmp_path / "existing.en.srt"
    source.write_text("subtitle", encoding="utf-8")
    output = tmp_path / "synced.en.srt"
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/test/ffsubsync")

    def fake_run(command, check=False):
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout="--skip-sync-on-low-quality", stderr="")
        output.write_bytes(source.read_bytes())
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "score: 123.0\noffset seconds: 31.0\nframerate scale factor: 1.0\n"
                "low-quality alignment (|offset| 31.0s > 30.0s); leaving subtitles unmodified\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(engine, "run", fake_run)
    result = engine._run_ffsubsync(
        tmp_path / "movie.mp4", source, output,
        {"ffsubsync_min_score": 0.0, "max_abs_offset_seconds": 600.0, "min_scale": 0.9, "max_scale": 1.1},
        0,
    )
    assert result["quality_rejected"] is True
    assert result["sync_applied"] is False
    assert result["quality_reasons"] == ["|offset| 31.0s > 30.0s"]


def test_existing_subtitle_is_not_replaced_after_low_quality_noop(monkeypatch, tmp_path):
    video = tmp_path / "Movie (2026).mp4"
    video.write_bytes(b"video")
    existing = tmp_path / "Movie (2026).en.srt"
    original = b"1\n00:00:01,000 --> 00:00:02,000\nAlready synchronized\n"
    existing.write_bytes(original)

    monkeypatch.setattr(engine, "_reference_audio_index", lambda video, language: (0, "only_audio_stream"))

    def fake_sync(video, source, output, cfg, reference_audio_index, **kwargs):
        output.write_bytes(source.read_bytes())
        return {
            "score": -324423.76,
            "offset_seconds": 0.0,
            "scale": 1.0,
            "skipped_cues": 0,
            "applied": False,
            "low_quality": True,
            "low_quality_reason": "score -324423.8 < 0.0",
        }

    monkeypatch.setattr(engine, "_run_ffsubsync", fake_sync)
    monkeypatch.setattr(engine, "_validate_srt", lambda path, duration: {"cue_count": 1, "last_cue_seconds": 2.0})
    work = tmp_path / "work"
    work.mkdir()

    status, details, commit_candidate = engine.process_subtitle_language(
        video,
        {"duration": 100.0, "media_type": "movie", "title": "Movie", "year": 2026, "season": None, "episode": None},
        "en",
        {"prefer_existing": True, "plain_srt_language": "en"},
        None,
        work,
    )

    assert status == StageStatus.NEEDS_REVIEW
    assert commit_candidate is None
    assert existing.read_bytes() == original
    assert details["classification"] == "FFSUBSYNC_LOW_QUALITY_NOOP"
    assert details["severity"] == "PRESERVED_UNVERIFIED"
    assert details["original_preserved"] is True
    assert details["candidate_installed"] is False
    assert not (work / "synced.en.srt").exists()


def test_cached_download_is_retained_but_not_installed_after_low_quality_noop(monkeypatch, tmp_path):
    video = tmp_path / "Movie (2026).mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    cached = work / "downloaded.en.srt"
    original = b"1\n00:00:01,000 --> 00:00:02,000\nDownloaded candidate\n"
    cached.write_bytes(original)

    monkeypatch.setattr(engine, "_extract_embedded_text_subtitle", lambda *args: (None, None))
    monkeypatch.setattr(engine, "_reference_audio_index", lambda *args: (0, "only_audio_stream"))

    def fake_sync(video, source, output, cfg, reference_audio_index, **kwargs):
        output.write_bytes(source.read_bytes())
        return {
            "score": -1.0, "offset_seconds": 0.0, "scale": 1.0,
            "sync_applied": False, "quality_rejected": True,
            "low_quality_reason": "score -1.0 < 0.0",
        }

    monkeypatch.setattr(engine, "_run_ffsubsync", fake_sync)
    monkeypatch.setattr(engine, "_validate_srt", lambda *args: {"cue_count": 1, "last_cue_seconds": 2.0})

    status, details, commit_candidate = engine.process_subtitle_language(
        video,
        {"duration": 100.0, "media_type": "movie", "title": "Movie", "year": 2026, "season": None, "episode": None},
        "en", {"prefer_existing": True, "plain_srt_language": "en"}, None, work,
    )

    assert status == StageStatus.NEEDS_REVIEW
    assert commit_candidate is None
    assert cached.read_bytes() == original
    assert details["source"] == "opensubtitles_cached"
    assert details["retained_candidate_path"] == str(cached)
    assert not video.with_name("Movie (2026).en.srt").exists()
    assert not (work / "synced.en.srt").exists()


def test_embedded_subtitle_does_not_create_sidecar_after_low_quality_noop(monkeypatch, tmp_path):
    video = tmp_path / "Movie (2026).mp4"
    video.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    extracted = work / "embedded.en.srt"
    extracted.write_text("1\n00:00:01,000 --> 00:00:02,000\nEmbedded\n", encoding="utf-8")

    monkeypatch.setattr(engine, "_extract_embedded_text_subtitle", lambda *args: (extracted, {"stream_index": 2}))
    monkeypatch.setattr(engine, "_reference_audio_index", lambda *args: (0, "only_audio_stream"))

    def fake_sync(video, source, output, cfg, reference_audio_index, **kwargs):
        output.write_bytes(source.read_bytes())
        return {
            "score": -1.0, "offset_seconds": 0.0, "scale": 1.0,
            "sync_applied": False, "quality_rejected": True,
            "low_quality_reason": "score -1.0 < 0.0",
        }

    monkeypatch.setattr(engine, "_run_ffsubsync", fake_sync)
    monkeypatch.setattr(engine, "_validate_srt", lambda *args: {"cue_count": 1, "last_cue_seconds": 2.0})

    status, details, commit_candidate = engine.process_subtitle_language(
        video,
        {"duration": 100.0, "media_type": "movie", "title": "Movie", "year": 2026, "season": None, "episode": None},
        "en", {"prefer_existing": True, "plain_srt_language": "en"}, None, work,
    )

    assert status == StageStatus.NEEDS_REVIEW
    assert commit_candidate is None
    assert details["source"] == "embedded"
    assert details["candidate_installed"] is False
    assert not video.with_name("Movie (2026).en.srt").exists()
    assert not (work / "synced.en.srt").exists()
    assert details["source_preserved"] is True
    assert details["output_committed"] is False


def test_wrong_release_runtime_is_review_not_failure_and_preserves_existing(monkeypatch, tmp_path):
    video = tmp_path / "Movie (2026).mp4"
    video.write_bytes(b"video")
    existing = tmp_path / "Movie (2026).en.srt"
    existing.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nOne\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n"
        "3\n00:04:59,000 --> 00:05:00,000\nThree\n",
        encoding="utf-8",
    )
    original = existing.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(engine, "_reference_audio_index", lambda *args: (0, "only_audio_stream"))

    def fake_sync(video, source, output, cfg, reference_audio_index, **kwargs):
        output.write_bytes(source.read_bytes())
        return {
            "score": 10.0, "offset_seconds": 0.0, "scale": 1.0,
            "quality_rejected": False, "low_quality": False,
            "candidate_validation_error": "Subtitle extends 200.0s beyond media duration",
            "method": "multi_segment_fallback",
        }

    monkeypatch.setattr(engine, "_run_ffsubsync", fake_sync)
    status, details, candidate = engine.process_subtitle_language(
        video,
        {"duration": 100.0, "media_type": "movie", "title": "Movie", "year": 2026,
         "season": None, "episode": None},
        "en", {"prefer_existing": True, "plain_srt_language": "en"}, None, work,
    )

    assert status == StageStatus.NEEDS_REVIEW
    assert candidate is None
    assert details["classification"] == "SUBTITLE_RELEASE_RUNTIME_MISMATCH"
    assert details["source_validation"]["valid"] is False
    assert details["original_preserved"] is True
    assert existing.read_bytes() == original
    assert not (work / "synced.en.srt").exists()


def test_runtime_regression_is_review_and_does_not_replace_plausible_source(monkeypatch, tmp_path):
    video = tmp_path / "Movie (2026).mp4"
    video.write_bytes(b"video")
    existing = tmp_path / "Movie (2026).en.srt"
    existing.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nOne\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nThree\n",
        encoding="utf-8",
    )
    original = existing.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(engine, "_reference_audio_index", lambda *args: (0, "only_audio_stream"))

    def fake_sync(video, source, output, cfg, reference_audio_index, **kwargs):
        output.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nOne\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n"
            "3\n00:04:59,000 --> 00:05:00,000\nThree\n",
            encoding="utf-8",
        )
        return {
            "score": 10.0, "offset_seconds": 0.0, "scale": 1.042709,
            "quality_rejected": False, "low_quality": False,
            "candidate_validation_error": "Subtitle extends 200.0s beyond media duration",
            "method": "multi_segment_fallback",
        }

    monkeypatch.setattr(engine, "_run_ffsubsync", fake_sync)
    status, details, candidate = engine.process_subtitle_language(
        video,
        {"duration": 100.0, "media_type": "movie", "title": "Movie", "year": 2026,
         "season": None, "episode": None},
        "en", {"prefer_existing": True, "plain_srt_language": "en"}, None, work,
    )

    assert status == StageStatus.NEEDS_REVIEW
    assert candidate is None
    assert details["classification"] == "FFSUBSYNC_RUNTIME_REGRESSION"
    assert details["source_validation"]["valid"] is True
    assert existing.read_bytes() == original
