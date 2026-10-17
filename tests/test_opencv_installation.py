from pathlib import Path
from types import SimpleNamespace
import sys
import tomllib

from media_manager.avsync import engine
from media_manager.models import VisualType


def test_installers_choose_same_single_opencv_distribution():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    extra = project["project"]["optional-dependencies"]["avsync"]
    docker = [line.strip() for line in (root / "requirements-avsync.txt").read_text().splitlines() if line and not line.startswith("#")]
    for dependencies in (extra, docker):
        assert [r for r in dependencies if r.startswith("opencv-")] == ["opencv-contrib-python>=4.8,<5"]
        assert "scenedetect>=0.6,<0.7" in dependencies
        assert not any("scenedetect[opencv" in r for r in dependencies)


def test_missing_detector_is_not_constructed_repeatedly(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        data=SimpleNamespace(haarcascades=str(tmp_path)),
        CascadeClassifier=lambda path: calls.append(path)))
    engine._face_cascade.cache_clear()
    try:
        assert engine._face_cascade() is None
        assert engine._face_cascade() is None
        assert calls == []
    finally:
        engine._face_cascade.cache_clear()


def test_missing_face_evidence_produces_unknown(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
    monkeypatch.setattr(engine, "_extract_frame", lambda *args: object())
    monkeypatch.setattr(engine, "_frame_features", lambda frame: (0.1, 0.9, 0.1, None))
    visual, confidence, details = engine.classify_visual_type(Path("unused"), SimpleNamespace(duration=100), {})
    assert visual == VisualType.UNKNOWN
    assert confidence == 0
    assert not details["face_evidence_available"]
