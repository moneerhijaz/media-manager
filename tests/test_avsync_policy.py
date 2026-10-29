from types import SimpleNamespace

import pytest

from media_manager.avsync import engine
from media_manager.models import AVSyncClassification, StageStatus, VisualType


def test_visual_classifier_samples_at_least_72(monkeypatch):
    import sys
    import types

    seen = []

    class DummyFrame:
        pass

    # The learned A/V stack is optional; this policy test should not require
    # OpenCV to be installed in the lean development environment.
    monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))
    monkeypatch.setattr(engine, "_extract_frame", lambda path, t: (seen.append(t) or DummyFrame()))
    monkeypatch.setattr(engine, "_frame_features", lambda frame: (0.8, 0.1, 0.9, True))
    progress = []
    probe = SimpleNamespace(duration=7200.0)
    visual, confidence, details = engine.classify_visual_type(
        SimpleNamespace(), probe, {"visual_sample_count": 24}, progress=progress.append
    )
    assert len(seen) == 72
    assert progress[-1]["completed"] == 72
    assert details["samples"] == 72
    assert visual.value == "live_action"
    assert confidence > 0


def test_speech_candidate_pool_is_larger_than_syncnet_target():
    segments = [(i * 100.0 + 10.0, i * 100.0 + 35.0) for i in range(40)]
    cfg = {
        "candidate_seconds": 10.0,
        "candidate_count": 12,
        "face_candidate_count": 36,
        "runtime_bins": 10,
    }
    candidates = engine._speech_candidates(segments, 4200.0, cfg)
    assert len(candidates) >= 30
    assert len(candidates) <= 36


def test_candidate_pool_can_reach_configured_adaptive_limit():
    from media_manager.config import DEFAULT_CONFIG

    candidates = engine._speech_candidates([(0.0, 7200.0)], 7200.0, DEFAULT_CONFIG["av_sync"])
    assert len(candidates) == 64


def test_saved_observations_reject_low_confidence_and_nonfinite_evidence():
    from media_manager.config import DEFAULT_CONFIG

    data = {"observations": [
        {"time": 100.0, "offset_frames": 2.0, "confidence": confidence}
        for confidence in (8.0, 1.0, float("nan"), float("inf"))
    ]}
    assert engine.evaluate_saved_observations(
        data, DEFAULT_CONFIG["av_sync"], duration=3600, visual_type=VisualType.UNKNOWN
    ) is None


def test_inconclusive_av_is_a_gap_and_preserves_evidence(tmp_path, monkeypatch):
    import json
    from media_manager import executor
    from media_manager.config import LibraryConfig
    from media_manager.database import Database
    from media_manager.models import AVAnalysis

    cfg = LibraryConfig.init(tmp_path)
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"test")
    monkeypatch.setattr(executor, "probe_media", lambda path: object())
    monkeypatch.setattr(executor, "analyze_av_sync", lambda *a, **kw: AVAnalysis(
        StageStatus.INSUFFICIENT_EVIDENCE, reason="No sustained speech candidates"))
    monkeypatch.setattr(executor, "apply_verified_av_correction", lambda *a, **kw: pytest.fail("Must not correct uncertain media"))
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = int(db.conn.execute(
            "INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,last_seen) "
            "VALUES('movie.mp4','movie','Movie',4,1,'fp',100,'{}','now')"
        ).lastrowid)
        for stage in ("identity", "integrity", "visual_type", "crop", "subtitle:en", "final_verify"):
            db.set_stage(media_id, stage, StageStatus.COMPLETE)
        executor._av_stage(source, db.get_media(media_id), cfg, db, tmp_path, VisualType.UNKNOWN)
        stage = db.get_stage(media_id, "av_sync")
        assert stage["status"] == "unavailable"
        assert json.loads(stage["details_json"])["analysis_status"] == "insufficient_evidence"
        assert executor._overall(db, media_id, ["en"]) == "managed_with_gaps"


def test_face_hint_uses_configurable_low_rate_sampling(monkeypatch, tmp_path):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(stdout=b"")

    # This command-construction test must not require the optional OpenCV extra.
    import sys
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
    monkeypatch.setattr(engine, "require_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(engine, "run", fake_run)

    assert engine._face_window_score(tmp_path / "movie.mp4", 10.0, 10.0, fps=2.0) == 0.0
    assert "-nostdin" in seen["command"]
    assert "fps=2,scale=640:-2" in seen["command"]


def test_legacy_unknown_av_result_is_retried():
    from media_manager.executor import _stage_needs_work
    stage = {
        "status": "insufficient_evidence",
        "details_json": '{"reason":"Visual type is unknown"}',
    }
    assert _stage_needs_work("av_sync", stage)


def test_stage_version_change_requeues_only_that_stage():
    from media_manager.executor import CROP_ALGORITHM_VERSION, _stage_needs_work
    old_crop = {"status": "not_needed", "details_json": '{"algorithm_version":"crop-v3"}'}
    current_crop = {"status": "not_needed", "details_json": '{"algorithm_version":"%s"}' % CROP_ALGORITHM_VERSION}
    assert _stage_needs_work("crop", old_crop)
    assert not _stage_needs_work("crop", current_crop)


def test_current_low_quality_subtitle_review_is_terminal():
    import json

    from media_manager.executor import SUBTITLE_ALGORITHM_VERSION, _stage_needs_work

    stage = {
        "status": "needs_review",
        "details_json": json.dumps({
            "algorithm_version": SUBTITLE_ALGORITHM_VERSION,
            "classification": "FFSUBSYNC_LOW_QUALITY_NOOP",
            "ffsubsync": {
                "method": "multi_segment_fallback",
                "multi_segment_fallback_attempted": True,
            },
        }),
    }
    assert not _stage_needs_work("subtitle:en", stage)


def test_v040_correction_review_migration_is_terminal_and_preserves_analysis(tmp_path):
    import json
    from media_manager.checkpoints import CheckpointStore
    from media_manager.config import LibraryConfig
    from media_manager.database import Database
    from media_manager.executor import (
        AVSYNC_ALGORITHM_VERSION,
        _stage_needs_work,
        _upgrade_legacy_av_correction_reviews,
    )
    from media_manager.models import StageStatus

    cfg = LibraryConfig.init(tmp_path / "library")
    with Database(cfg.state_dir / "media-manager.db") as db:
        media_id = int(db.conn.execute(
            """INSERT INTO media(rel_path,media_type,title,size,mtime_ns,fingerprint,duration,probe_json,overall_status,last_seen)
               VALUES('Movies/Example Film/Example Film.mp4','movie','Example Film',1,1,'fp',1.0,'{}','needs_review','now')"""
        ).lastrowid)
        db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, {
            "reason": "A/V processing failed after checkpointed analysis",
            "error": "Proposed A/V correction did not validate (original 120.0 ms, residual 110.0 ms)",
        })
        store = CheckpointStore(cfg.state_dir, media_id)
        store.save("av_sync", {
            "config_key": json.dumps({"algorithm": AVSYNC_ALGORITHM_VERSION}),
            "phase": "analysis_complete",
            "final_analysis": {
                "status": "complete",
                "observations": [{"time": 10.0, "offset_frames": 3.0, "confidence": 8.0, "min_distance": 2.0, "face_score": 0.9}],
                "median_offset_frames": 3.0,
                "median_offset_ms": 120.0,
                "spread_ms": 5.0,
                "drift_ms_per_hour": 1.0,
                "confidence": 8.0,
                "reason": "Consistent high-confidence global A/V offset",
                "reference_audio_index": 0,
            },
        })
        assert _upgrade_legacy_av_correction_reviews(cfg, db) == 1
        stage = db.get_stage(media_id, "av_sync")
        details = json.loads(stage["details_json"])
        assert details["algorithm_version"] == AVSYNC_ALGORITHM_VERSION
        assert details["analysis_checkpoint_preserved"] is True
        assert details["median_offset_ms"] == 120.0
        assert "did not validate" in details["correction_error"]
        assert not _stage_needs_work("av_sync", stage)


def test_silero_vad_streams_ffmpeg_pcm_without_torchaudio_file_io(monkeypatch, tmp_path):
    import io
    import sys
    import types
    import numpy as np

    # Four Silero 32 ms frames. The fake iterator opens speech on frame 1 and
    # closes it on frame 4. Deliberately provide no read_audio symbol: this
    # regression fails if media-manager ever goes back to Silero's torchaudio
    # convenience loader.
    raw = np.zeros(512 * 4, dtype="<f4").tobytes()

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(raw)
            self.stderr = io.BytesIO(b"")
            self.returncode = None

        def wait(self):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class FakeVADIterator:
        def __init__(self, model, sampling_rate=16000):
            self.calls = 0

        def __call__(self, tensor, return_seconds=False):
            self.calls += 1
            if self.calls == 1:
                return {"start": 0}
            if self.calls == 4:
                return {"end": 512 * 4}
            return None

    fake_silero = types.ModuleType("silero_vad")
    fake_silero.load_silero_vad = lambda: object()
    fake_silero.VADIterator = FakeVADIterator
    fake_torch = types.ModuleType("torch")
    fake_torch.from_numpy = lambda values: values
    fake_torch.get_num_threads = lambda: 8
    restored_threads = []
    fake_torch.set_num_threads = restored_threads.append
    monkeypatch.setitem(sys.modules, "silero_vad", fake_silero)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(engine, "require_tool", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(engine.subprocess, "Popen", FakeProcess)

    progress = []
    segments = engine._silero_speech(
        tmp_path / "movie.mp4", 0, tmp_path, duration=1.0, progress=progress.append
    )
    assert segments == [(0.0, 0.128)] or segments == []  # 128 ms is filtered by the 250 ms evidence floor.
    assert progress
    assert progress[-1]["message"].endswith("no TorchCodec)")
    assert progress[-1]["completed"] > 0
    assert restored_threads == [8]


def test_avsync_algorithm_version_invalidates_old_torchcodec_failure():
    from media_manager.executor import AVSYNC_ALGORITHM_VERSION, _stage_needs_work
    assert AVSYNC_ALGORITHM_VERSION == "avsync-v10-drift-policy"
    old = {
        "status": "needs_review",
        "details_json": '{"algorithm_version":"avsync-v4-speech-syncnet-evidence","error":"torchaudio requires torchcodec"}',
    }
    assert _stage_needs_work("av_sync", old)


def test_robust_drift_ignores_one_outlier_and_requires_runtime_coverage():
    from media_manager.models import AVObservation

    observations = [
        AVObservation(time, frames, 8.0)
        for time, frames in [(0, 2), (900, 2), (1800, 20), (2700, 2), (3600, 2)]
    ]
    drift, evidence = engine._robust_drift(observations, 3600.0)
    assert abs(drift) < 1.0
    assert evidence["actionable"] is True

    short_span = [AVObservation(float(i * 10), float(i), 8.0) for i in range(5)]
    drift, evidence = engine._robust_drift(short_span, 3600.0)
    assert drift > 80.0
    assert evidence["actionable"] is False


def test_saved_observations_use_current_policy_without_visual_hard_gate():
    from media_manager.config import DEFAULT_CONFIG

    data = {
        "reference_audio_index": 0,
        "observations": [
            {"time": time, "offset_frames": 3.0, "confidence": 8.0}
            for time in (100.0, 1800.0, 3400.0)
        ],
    }
    result = engine.evaluate_saved_observations(
        data, DEFAULT_CONFIG["av_sync"], duration=3600.0, visual_type=VisualType.ANIMATION
    )
    assert result is not None
    assert result.status == StageStatus.COMPLETE


def test_long_speech_segment_generates_multiple_overlapping_windows():
    cfg = {
        "candidate_seconds": 10.0,
        "candidate_count": 20,
        "candidate_max_count": 64,
        "face_candidate_count": 96,
        "runtime_bins": 24,
    }
    candidates = engine._speech_candidates([(100.0, 160.0)], 1000.0, cfg)
    assert len(candidates) >= 8
    starts = [round(x[0], 1) for x in candidates]
    assert min(starts) <= 101.0
    assert max(starts) >= 149.0


def test_syncnet_diagnostics_reports_low_confidence_distribution():
    results = {
        "1": {"ok": True, "confidence": 2.0},
        "2": {"ok": True, "confidence": 4.49},
        "3": {"ok": True, "confidence": 5.2},
        "4": {"ok": False, "error": "no face"},
    }
    d = engine._syncnet_diagnostics(results, 4.5)
    assert d["tested"] == 4
    assert d["successful"] == 3
    assert d["failed"] == 1
    assert d["high_confidence"] == 1
    assert d["best_confidence"] == 5.2
    assert d["confidence_threshold"] == 4.5
    assert d["failure_reasons"]["no face"] == 1



def test_syncnet_backend_passes_explicit_audio_path(monkeypatch, tmp_path):
    import sys
    import types
    from pathlib import Path

    seen = {}

    class FakePipeline:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def inference(self, *, video_path, audio_path, cache_dir=None):
            assert audio_path is not None
            ap = Path(audio_path)
            assert ap.exists()
            assert ap.stat().st_size > 44
            seen["video_path"] = video_path
            seen["audio_path"] = audio_path
            seen["cache_dir"] = cache_dir
            return ([2], [6.25], [3.5], 6.25, 3.5, "[]", True)

    fake = types.ModuleType("syncnet_python")
    fake.SyncNetPipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "syncnet_python", fake)
    monkeypatch.setattr(engine, "require_tool", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, *args, **kwargs):
        # The backend's explicit audio extraction writes to the final command arg.
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF" + b"0" * 128)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(engine, "run", fake_run)
    clip = tmp_path / "candidate.mkv"
    clip.write_bytes(b"candidate")
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "sfd_face.pth").write_bytes(b"x")
    (weights / "syncnet_v2.model").write_bytes(b"x")
    backend = engine.SyncNetBackend(tmp_path, {
        "syncnet_s3fd_weights": "weights/sfd_face.pth",
        "syncnet_weights": "weights/syncnet_v2.model",
        "syncnet_device": "cpu",
    })
    offset, confidence, distance = backend.evaluate(clip)
    assert offset == 2.0
    assert confidence == 6.25
    assert distance == 3.5
    assert seen["audio_path"].endswith("candidate.wav")
    assert seen["cache_dir"] is not None


def test_syncnet_runtime_output_is_contained_without_hiding_stderr_progress(monkeypatch, capsys):
    import ffmpeg
    import logging
    import sys

    seen = {}

    def fake_ffmpeg_run(stream, *args, **kwargs):
        seen.update(kwargs)
        print("upstream ffmpeg stdout")
        print("upstream ffmpeg stderr", file=sys.stderr)
        return b"", b""

    monkeypatch.setattr(engine, "_bounded_ffmpeg_run", fake_ffmpeg_run)
    with engine._quiet_syncnet_output():
        print("upstream SyncNet frame array")
        logging.getLogger("pyscenedetect").error("upstream scene warning")
        ffmpeg.input("input.mkv").output("output.avi").run()
        print("media-manager progress", file=sys.stderr)

    captured = capsys.readouterr()
    assert seen["quiet"] is True
    assert captured.out == ""
    assert "upstream SyncNet" not in captured.out
    assert "upstream scene warning" not in captured.err
    assert "media-manager progress" in captured.err


def test_syncnet_ffmpeg_wrapper_removes_legacy_async_and_starts_new_session(monkeypatch):
    import ffmpeg

    seen = {}

    class FinishedProcess:
        pid = 12345
        returncode = 0

        def communicate(self, value, timeout):
            seen["input"] = value
            seen["timeout"] = timeout
            return b"", b""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        ffmpeg,
        "compile",
        lambda *args, **kwargs: ["ffmpeg", "-i", "clip.mkv", "-async", "1", "-q:v", "2", "video.avi"],
    )

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["popen"] = kwargs
        return FinishedProcess()

    monkeypatch.setattr(engine.subprocess, "Popen", fake_popen)
    engine._bounded_ffmpeg_run(object(), timeout_seconds=12.0, max_video_width=1920, quiet=True)

    assert "-async" not in seen["command"]
    assert "1" not in seen["command"]
    assert seen["command"][1] == "-nostdin"
    assert "-an" in seen["command"]
    assert "scale=w='min(1920,iw)':h=-2" in seen["command"]
    assert seen["popen"]["start_new_session"] is True
    assert seen["timeout"] == 12.0


def test_syncnet_ffmpeg_wrapper_does_not_scale_video_avi_input(monkeypatch):
    import ffmpeg

    seen = {}

    class FinishedProcess:
        pid = 12345
        returncode = 0

        def communicate(self, value, timeout):
            return b"", b""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(
        ffmpeg,
        "compile",
        lambda *args, **kwargs: [
            "ffmpeg", "-i", "/tmp/cache/video.avi", "-f", "image2",
            "/tmp/cache/frames/%06d.jpg", "-y",
        ],
    )

    def fake_popen(command, **kwargs):
        seen["command"] = command
        return FinishedProcess()

    monkeypatch.setattr(engine.subprocess, "Popen", fake_popen)
    engine._bounded_ffmpeg_run(object(), timeout_seconds=12.0, max_video_width=1920, quiet=True)

    assert "-vf" not in seen["command"]
    assert "-an" not in seen["command"]


def test_syncnet_ffmpeg_timeout_terminates_process_group(monkeypatch):
    import ffmpeg

    stopped = []

    class HangingProcess:
        pid = 54321
        returncode = None

        def communicate(self, value, timeout):
            raise engine.subprocess.TimeoutExpired(["ffmpeg"], timeout)

        def poll(self):
            return self.returncode

    monkeypatch.setattr(ffmpeg, "compile", lambda *args, **kwargs: ["ffmpeg", "-i", "clip.mkv", "video.avi"])
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *args, **kwargs: HangingProcess())
    monkeypatch.setattr(engine, "_terminate_process_group", lambda proc: stopped.append(proc.pid))

    with pytest.raises(engine.SyncNetAdapterError) as caught:
        engine._bounded_ffmpeg_run(object(), timeout_seconds=0.1, quiet=True)

    assert stopped == [54321]
    assert caught.value.phase == "syncnet_ffmpeg_timeout"
    assert caught.value.classification == AVSyncClassification.CANDIDATE_PREPARATION_FAILED


def test_process_group_cleanup_escalates_when_ffmpeg_ignores_term(monkeypatch):
    sent = []

    class StuckProcess:
        pid = 24680

        def poll(self):
            return None

        def wait(self, timeout):
            if len(sent) == 1:
                raise engine.subprocess.TimeoutExpired(["ffmpeg"], timeout)
            return -9

    monkeypatch.setattr(engine.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    engine._terminate_process_group(StuckProcess(), grace_seconds=0.01)
    assert sent == [
        (24680, engine.signal.SIGTERM),
        (24680, engine.signal.SIGKILL),
    ]


def test_syncnet_candidate_deadline_is_a_classified_failure():
    import time

    with pytest.raises(engine.SyncNetAdapterError) as caught:
        with engine._syncnet_candidate_deadline(0.05):
            time.sleep(0.2)

    assert caught.value.phase == "syncnet_candidate_timeout"
    assert caught.value.classification == AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION


def test_syncnet_progress_message_does_not_compare_count_to_threshold():
    message = engine._syncnet_progress_message({
        "high_confidence": 0,
        "successful": 1,
        "no_face": 1,
        "failed": 0,
        "best_confidence": 2.969,
    }, 4.5)
    assert message == "strong=0/3 usable=1 no-face=1 errors=0 best=2.97 threshold=4.5"
    assert ">=" not in message


def test_syncnet_runtime_output_restores_process_state_after_interrupt(monkeypatch):
    from ffmpeg.nodes import OutputStream
    import logging
    import sys

    def fake_ffmpeg_run(stream, *args, **kwargs):
        return b"", b""

    monkeypatch.setattr(OutputStream, "run", fake_ffmpeg_run)
    original_stdout = sys.stdout
    original_logging_disable = logging.root.manager.disable

    with pytest.raises(KeyboardInterrupt):
        with engine._quiet_syncnet_output():
            raise KeyboardInterrupt

    assert OutputStream.run is fake_ffmpeg_run
    assert sys.stdout is original_stdout
    assert logging.root.manager.disable == original_logging_disable


def _backend_config():
    return {
        "syncnet_s3fd_weights": "weights/sfd_face.pth",
        "syncnet_weights": "weights/syncnet_v2.model",
        "syncnet_device": "cpu",
    }


def _fake_weights(tmp_path):
    weights = tmp_path / "weights"
    weights.mkdir(exist_ok=True)
    (weights / "sfd_face.pth").write_bytes(b"s3fd")
    (weights / "syncnet_v2.model").write_bytes(b"syncnet")


def test_syncnet_noncallable_public_export_exposes_hidden_import(monkeypatch, tmp_path):
    import types

    fake = types.ModuleType("syncnet_python")
    fake.SyncNetPipeline = None

    def fake_import(name):
        if name == "syncnet_python":
            return fake
        if name == "syncnet_python.syncnet_pipeline":
            raise ModuleNotFoundError(
                "No module named 'scenedetect.video_manager'",
                name="scenedetect.video_manager",
            )
        raise AssertionError(name)

    monkeypatch.setattr(engine.importlib, "import_module", fake_import)
    backend = engine.SyncNetBackend(tmp_path, _backend_config())
    with pytest.raises(engine.SyncNetAdapterError) as caught:
        backend.prepare()
    assert caught.value.phase == "syncnet_import"
    assert caught.value.classification == AVSyncClassification.BACKEND_UNAVAILABLE
    assert "scenedetect.video_manager" in str(caught.value)
    assert "scenedetect>=0.6,<0.7" in str(caught.value)
    assert "NoneType' object is not callable" not in str(caught.value)


def test_syncnet_constructor_failure_is_attempted_only_once(monkeypatch, tmp_path):
    import sys
    import types

    attempts = 0

    class BrokenPipeline:
        def __init__(self, **kwargs):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("bad model factory")

    fake = types.ModuleType("syncnet_python")
    fake.SyncNetPipeline = BrokenPipeline
    monkeypatch.setitem(sys.modules, "syncnet_python", fake)
    _fake_weights(tmp_path)
    backend = engine.SyncNetBackend(tmp_path, _backend_config())

    for _ in range(2):
        with pytest.raises(engine.SyncNetAdapterError) as caught:
            backend.prepare()
        assert caught.value.phase == "syncnet_pipeline_init"
        assert caught.value.exception_type == "RuntimeError"
    assert attempts == 1


def test_syncnet_seven_tuple_parser_selects_matching_best_track():
    result = ([1, -3, 2], [4.8, 7.25, 5.0], [6.0, 2.5, 4.0], 7.25, 2.5, "[]", True)
    assert engine._parse_syncnet_result(result) == (-3.0, 7.25, 2.5)


@pytest.mark.parametrize(
    "result",
    [
        ([1], [5.0]),
        {"offsets": [1]},
        ([1, 2], [5.0], [3.0, 4.0], 5.0, 3.0, "[]", True),
        ([1], ["not-a-number"], [3.0], 5.0, 3.0, "[]", True),
    ],
)
def test_syncnet_unexpected_result_format_is_clear(result):
    with pytest.raises(engine.SyncNetAdapterError) as caught:
        engine._parse_syncnet_result(result)
    assert caught.value.phase == "syncnet_result_parse"
    assert caught.value.classification == AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION
    assert "Unexpected SyncNet result format" in str(caught.value)


def test_syncnet_no_face_is_a_valid_non_failure_result():
    assert engine._parse_syncnet_result(([], [], [], 0.0, 0.0, "", False)) is None
    diagnostics = engine._syncnet_diagnostics({
        "1": {
            "ok": False,
            "valid_result": True,
            "classification": AVSyncClassification.SYNCNET_NO_FACE.value,
        },
    }, 4.5)
    assert diagnostics["valid_results"] == 1
    assert diagnostics["no_face"] == 1
    assert diagnostics["failed"] == 0


def _analysis_config(candidate_max_count=20):
    return {
        "authoritative_language": "eng",
        "candidate_seconds": 10.0,
        "candidate_count": 20,
        "candidate_max_count": candidate_max_count,
        "face_candidate_count": candidate_max_count,
        "runtime_bins": 4,
        "min_confidence": 4.5,
        "not_needed_threshold_ms": 45.0,
        "max_auto_offset_ms": 500.0,
        "max_spread_ms": 80.0,
        "max_drift_ms_per_hour": 80.0,
        **_backend_config(),
    }


def test_adaptive_syncnet_stops_after_eight_strong_candidates(monkeypatch, tmp_path):
    calls = {"evaluate": 0}

    class StrongBackend:
        def __init__(self, root, cfg):
            pass

        def prepare(self):
            return self

        def evaluate(self, clip):
            calls["evaluate"] += 1
            return (0.0, 8.0, 2.0)

        def environment(self):
            return {"device": "cpu"}

    candidates = [(float(i * 20), 1.0) for i in range(20)]
    config = _analysis_config(candidate_max_count=20)
    config["candidate_count"] = 8
    config["face_candidate_count"] = 20
    monkeypatch.setattr(engine, "SyncNetBackend", StrongBackend)
    monkeypatch.setattr(engine, "choose_authoritative_audio", lambda probe, language: (0, "test"))
    monkeypatch.setattr(engine, "source_signature", lambda path: {"test": True})
    monkeypatch.setattr(engine, "_silero_speech", lambda *args, **kwargs: [(0.0, 500.0)])
    monkeypatch.setattr(engine, "_speech_candidates", lambda *args, **kwargs: candidates)
    monkeypatch.setattr(engine, "_face_window_score", lambda *args, **kwargs: 0.5)
    monkeypatch.setattr(engine, "_make_analysis_clip", lambda *args, **kwargs: None)

    analysis = engine.analyze_av_sync(
        tmp_path / "movie.mp4",
        SimpleNamespace(duration=1000.0),
        tmp_path,
        config,
        VisualType.UNKNOWN,
        work_dir=tmp_path / "work",
    )

    assert calls["evaluate"] == 8
    assert analysis.diagnostics["tested"] == 8
    assert analysis.diagnostics["high_confidence"] == 8


def test_identical_syncnet_inference_failures_trip_circuit_breaker(monkeypatch, tmp_path):
    calls = {"prepare": 0, "evaluate": 0}

    class FailingBackend:
        def __init__(self, root, cfg):
            pass

        def prepare(self):
            calls["prepare"] += 1
            return self

        def evaluate(self, clip):
            calls["evaluate"] += 1
            raise TypeError("'NoneType' object is not callable")

        def environment(self):
            return {
                "syncnet_version": "test",
                "syncnet_module_path": "/test/syncnet_python/__init__.py",
                "python_version": "test",
                "torch_version": "test",
                "scenedetect_version": "test",
                "device": "cpu",
            }

    candidates = [(float(i * 20), 1.0) for i in range(20)]
    monkeypatch.setattr(engine, "SyncNetBackend", FailingBackend)
    monkeypatch.setattr(engine, "choose_authoritative_audio", lambda probe, language: (0, "test"))
    monkeypatch.setattr(engine, "source_signature", lambda path: {"test": True})
    monkeypatch.setattr(engine, "_silero_speech", lambda *args, **kwargs: [(0.0, 500.0)])
    monkeypatch.setattr(engine, "_speech_candidates", lambda *args, **kwargs: candidates)
    monkeypatch.setattr(engine, "_face_window_score", lambda *args, **kwargs: 0.5)
    monkeypatch.setattr(engine, "_make_analysis_clip", lambda *args, **kwargs: None)

    analysis = engine.analyze_av_sync(
        tmp_path / "movie.mp4",
        SimpleNamespace(duration=1000.0),
        tmp_path,
        _analysis_config(),
        VisualType.UNKNOWN,
        work_dir=tmp_path / "work",
    )

    assert calls == {"prepare": 1, "evaluate": 3}
    assert analysis.status == StageStatus.NEEDS_REVIEW
    assert analysis.classification == AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION
    assert "3/3 consecutive candidates failed identically" in analysis.reason
    assert "high-confidence" not in analysis.reason
    assert analysis.diagnostics["successful"] == 0
    assert analysis.diagnostics["failed"] == 3
    assert analysis.diagnostics["circuit_breaker"]["triggered"] is True
    assert len(analysis.diagnostics["failure_groups"]) == 1
    assert analysis.diagnostics["failure_groups"][0]["count"] == 3
    assert "TypeError" in analysis.diagnostics["failure_groups"][0]["traceback"]


def test_circuit_breaker_overrides_prior_high_confidence_results(monkeypatch, tmp_path):
    calls = {"evaluate": 0}

    class MixedBackend:
        def __init__(self, root, cfg):
            pass

        def prepare(self):
            return self

        def evaluate(self, clip):
            calls["evaluate"] += 1
            if calls["evaluate"] <= 3:
                return (0.0, 8.0, 2.0)
            raise TypeError("'NoneType' object is not callable")

        def environment(self):
            return {
                "syncnet_version": "test",
                "syncnet_module_path": "/test/syncnet_python/__init__.py",
                "python_version": "test",
                "torch_version": "test",
                "scenedetect_version": "test",
                "device": "cpu",
            }

    candidates = [(float(i * 20), 1.0) for i in range(20)]
    monkeypatch.setattr(engine, "SyncNetBackend", MixedBackend)
    monkeypatch.setattr(engine, "choose_authoritative_audio", lambda probe, language: (0, "test"))
    monkeypatch.setattr(engine, "source_signature", lambda path: {"test": True})
    monkeypatch.setattr(engine, "_silero_speech", lambda *args, **kwargs: [(0.0, 500.0)])
    monkeypatch.setattr(engine, "_speech_candidates", lambda *args, **kwargs: candidates)
    monkeypatch.setattr(engine, "_face_window_score", lambda *args, **kwargs: 0.5)
    monkeypatch.setattr(engine, "_make_analysis_clip", lambda *args, **kwargs: None)

    analysis = engine.analyze_av_sync(
        tmp_path / "movie.mp4",
        SimpleNamespace(duration=1000.0),
        tmp_path,
        _analysis_config(),
        VisualType.UNKNOWN,
        work_dir=tmp_path / "work",
    )

    assert calls["evaluate"] == 6
    assert len(analysis.observations) == 3
    assert analysis.status == StageStatus.NEEDS_REVIEW
    assert analysis.classification == AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION
    assert analysis.diagnostics["successful"] == 3
    assert analysis.diagnostics["failed"] == 3
    assert analysis.diagnostics["circuit_breaker"]["triggered"] is True
    assert "3 valid confidence results" in analysis.reason
