from __future__ import annotations

import contextlib
import json
import hashlib
import importlib
import importlib.metadata
import logging
import math
import os
import platform
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from ..checkpoints import CheckpointStore, source_signature
from ..models import AVAnalysis, AVObservation, AVSyncClassification, ProbeInfo, StageStatus, VisualType
from ..probe import choose_authoritative_audio
from ..utils import MediaManagerError, require_tool, run


SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
AVSYNC_ALGORITHM_VERSION = "avsync-v8-syncnet-adapter-fix"


class SyncNetAdapterError(MediaManagerError):
    """A phase-aware boundary error from the third-party SyncNet adapter."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        classification: AVSyncClassification,
        cause: BaseException | None = None,
    ):
        super().__init__(message)
        self.phase = phase
        self.classification = classification
        self.exception_type = type(cause).__name__ if cause is not None else type(self).__name__


def _extract_frame(path: Path, t: float, width: int = 320):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    ffmpeg = require_tool("ffmpeg")
    proc = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0,t):.3f}",
        "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ], text=False)
    if not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _frame_features(frame) -> tuple[float, float, float, bool]:
    """Return color complexity, edge density, texture variance, face-present."""
    import cv2
    import numpy as np

    small = cv2.resize(frame, (160, max(2, int(frame.shape[0] * 160 / frame.shape[1]))))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    # Quantized color diversity; normalized to [0,1].
    q = (hsv // np.array([12, 32, 32], dtype=np.uint8)).reshape(-1, 3)
    uniq = len(np.unique(q, axis=0))
    complexity = min(1.0, uniq / 700.0)
    edges = cv2.Canny(gray, 80, 160)
    edge_density = float((edges > 0).mean())
    texture = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 1200.0)
    face = False
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(24, 24))
        face = len(faces) > 0
    except Exception:
        pass
    return complexity, edge_density, texture, face


def classify_visual_type(
    path: Path,
    probe: ProbeInfo,
    cfg: dict,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[VisualType, float, dict[str, Any]]:
    """Conservative CPU heuristic. Strongly classifies only clear cases; otherwise UNKNOWN."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return VisualType.UNKNOWN, 0.0, {"reason": "opencv-python-headless not installed"}
    count = max(72, int(cfg.get("visual_sample_count", 72)))
    times = [probe.duration * (i + 0.5) / count for i in range(count)]
    feats = []
    for i, t in enumerate(times):
        frame = _extract_frame(path, t)
        if frame is not None:
            feats.append(_frame_features(frame))
        if progress:
            progress({"phase": "visual_sampling", "completed": i + 1, "total": count, "usable": len(feats)})
    if len(feats) < max(4, count // 4):
        return VisualType.UNKNOWN, 0.0, {"reason": "too few visual samples"}
    complexity = statistics.median(x[0] for x in feats)
    edges = statistics.median(x[1] for x in feats)
    texture = statistics.median(x[2] for x in feats)
    face_rate = sum(1 for x in feats if x[3]) / len(feats)

    # Natural photographic imagery tends to have high texture/color complexity;
    # cartoons tend to have lower complexity with strong clean edges. These are
    # deliberately conservative thresholds, not a semantic classifier.
    live_score = 0.45 * complexity + 0.35 * texture + 0.20 * min(1.0, face_rate * 3)
    animation_score = 0.50 * max(0.0, 1.0 - complexity) + 0.35 * min(1.0, edges / 0.16) + 0.15 * max(0.0, 1.0 - texture)
    details = {
        "samples": len(feats), "color_complexity": complexity, "edge_density": edges,
        "texture": texture, "face_rate": face_rate,
        "live_score": live_score, "animation_score": animation_score,
        "method": "conservative_opencv_heuristic",
    }
    if live_score >= 0.67 and live_score - animation_score >= 0.18:
        return VisualType.LIVE_ACTION, live_score, details
    if animation_score >= 0.68 and animation_score - live_score >= 0.20:
        return VisualType.ANIMATION, animation_score, details
    # Mixed is only claimed if both signals are high; otherwise unknown is safer.
    if live_score >= 0.58 and animation_score >= 0.58:
        return VisualType.MIXED, min(live_score, animation_score), details
    return VisualType.UNKNOWN, max(live_score, animation_score), details


def _read_exact(stream, size: int) -> bytes:
    """Read up to *size* bytes from a pipe, tolerating short pipe reads."""
    parts: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _silero_speech(
    path: Path,
    audio_index: int,
    work: Path,
    *,
    duration: float | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[tuple[float, float]] | None:
    """Find speech with Silero without using torchaudio/TorchCodec file I/O.

    FFmpeg decodes the selected stream to mono 16 kHz float PCM and the samples
    are fed directly to Silero's streaming VAD iterator.  This keeps the audio
    path independent of torchaudio.load(), which requires TorchCodec in modern
    torchaudio releases, and avoids holding a multi-hour movie soundtrack in
    memory at once.
    """
    del work  # retained in the signature for compatibility with older callers
    try:
        import numpy as np
        import torch
    except ImportError:
        return None

    # silero_vad.model sets PyTorch's process-global thread count to one at
    # import time.  That is reasonable for tiny VAD frames but makes the CPU
    # SyncNet face detector several times slower later in the same process.
    # Preserve the caller's setting and restore it after the speech scan.
    previous_torch_threads = torch.get_num_threads()
    try:
        try:
            from silero_vad import VADIterator, load_silero_vad
        except ImportError:
            return None
        return _silero_speech_loaded(
            path,
            audio_index,
            duration=duration,
            progress=progress,
            np=np,
            torch=torch,
            vad_iterator_class=VADIterator,
            load_model=load_silero_vad,
        )
    finally:
        torch.set_num_threads(previous_torch_threads)


def _silero_speech_loaded(
    path: Path,
    audio_index: int,
    *,
    duration: float | None,
    progress: Callable[[dict[str, Any]], None] | None,
    np: Any,
    torch: Any,
    vad_iterator_class: Any,
    load_model: Callable[[], Any],
) -> list[tuple[float, float]]:

    ffmpeg = require_tool("ffmpeg")
    sample_rate = 16000
    frame_samples = 512  # Silero's native 32 ms window at 16 kHz.
    frame_bytes = frame_samples * 4  # pcm_f32le
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(path), "-map", f"0:a:{audio_index}",
        "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stdout is None:
        proc.kill()
        raise MediaManagerError("FFmpeg did not expose PCM output for Silero VAD")

    model = load_model()
    vad = vad_iterator_class(model, sampling_rate=sample_rate)
    segments: list[tuple[float, float]] = []
    open_start: float | None = None
    actual_samples = 0
    last_progress_second = -1

    try:
        while True:
            raw = _read_exact(proc.stdout, frame_bytes)
            if not raw:
                break
            usable = len(raw) - (len(raw) % 4)
            if usable <= 0:
                break
            if usable != len(raw):
                raw = raw[:usable]
            values = np.frombuffer(raw, dtype="<f4").copy()
            actual_count = int(values.size)
            actual_samples += actual_count
            if actual_count < frame_samples:
                values = np.pad(values, (0, frame_samples - actual_count))
            tensor = torch.from_numpy(values)
            event = vad(tensor, return_seconds=False)
            if event:
                if "start" in event:
                    open_start = max(0.0, float(event["start"]) / sample_rate)
                if "end" in event and open_start is not None:
                    end = min(float(event["end"]) / sample_rate, actual_samples / sample_rate)
                    if end - open_start >= 0.25:
                        segments.append((open_start, end))
                    open_start = None

            if progress:
                current_second = int(actual_samples / sample_rate)
                if current_second != last_progress_second:
                    last_progress_second = current_second
                    progress({
                        "phase": "speech_scan",
                        "completed": actual_samples / sample_rate,
                        "total": duration,
                        "segments": len(segments),
                        "message": "Silero speech scan (FFmpeg PCM; no TorchCodec)",
                    })

        rc = proc.wait()
        stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", errors="replace")
        if rc != 0:
            raise MediaManagerError(f"FFmpeg PCM decode for Silero VAD failed: {stderr.strip() or f'exit {rc}'}")
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise

    if open_start is not None:
        end = actual_samples / sample_rate
        if end - open_start >= 0.25:
            segments.append((open_start, end))
    return segments


def _ffmpeg_nonsilence(path: Path, audio_index: int, duration: float) -> list[tuple[float, float]]:
    ffmpeg = require_tool("ffmpeg")
    proc = run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(path),
        "-map", f"0:a:{audio_index}", "-af", "silencedetect=noise=-35dB:d=0.5",
        "-f", "null", "/dev/null",
    ], check=False)
    text = proc.stderr or ""
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in text.splitlines():
        m = SILENCE_START_RE.search(line)
        if m:
            pending = float(m.group(1))
        m = SILENCE_END_RE.search(line)
        if m and pending is not None:
            silences.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:
        silences.append((pending, duration))
    speech: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in sorted(silences):
        if a > cur:
            speech.append((cur, a))
        cur = max(cur, b)
    if cur < duration:
        speech.append((cur, duration))
    return [(a, b) for a, b in speech if b - a >= 1.0]


def _speech_candidates(segments: list[tuple[float, float]], duration: float, cfg: dict) -> list[tuple[float, float]]:
    """Generate a broad, runtime-distributed pool of speech-heavy windows.

    Long VAD segments contribute multiple overlapping windows instead of only
    one center point. This matters for feature films where one part of a long
    dialogue scene may have an off-screen speaker while another has a clean
    frontal speaking face.
    """
    win = float(cfg["candidate_seconds"])
    bins = max(12, int(cfg.get("runtime_bins", 24)))
    target = max(1, int(cfg.get("face_candidate_count", max(48, int(cfg.get("candidate_max_count", 64)) * 2))))
    step = max(2.0, win * 0.5)
    raw: list[tuple[float, float]] = []
    max_start = max(0.0, duration - win)
    for a, b in segments:
        a = max(0.0, float(a)); b = min(float(duration), float(b))
        if b - a < 1.5:
            continue
        starts: list[float] = []
        if b - a <= win:
            starts = [max(0.0, min(max_start, (a + b) / 2 - win / 2))]
        else:
            cur = a
            while cur <= b - win + 1e-6:
                starts.append(max(0.0, min(max_start, cur)))
                cur += step
            tail = max(0.0, min(max_start, b - win))
            if not starts or abs(starts[-1] - tail) > 0.5:
                starts.append(tail)
        for st in starts:
            overlap = max(0.0, min(st + win, b) - max(st, a)) / max(win, 0.001)
            raw.append((st, overlap))
    if not raw:
        return []

    # De-duplicate near-identical starts while keeping the stronger speech occupancy.
    dedup: dict[int, tuple[float, float]] = {}
    for st, score in raw:
        key = int(round(st * 2.0))  # 0.5-second buckets
        if key not in dedup or score > dedup[key][1]:
            dedup[key] = (st, score)
    raw = list(dedup.values())

    # Guarantee runtime coverage first, then fill with the strongest speech windows.
    per_bin: dict[int, tuple[float, float]] = {}
    for item in raw:
        idx = min(bins - 1, int((item[0] / max(duration, 0.001)) * bins))
        if idx not in per_bin or item[1] > per_bin[idx][1]:
            per_bin[idx] = item
    selected = list(per_bin.values())
    for item in sorted(raw, key=lambda x: x[1], reverse=True):
        if len(selected) >= target:
            break
        if all(abs(item[0] - x[0]) > min(2.0, win * 0.2) for x in selected):
            selected.append(item)
    return sorted(selected[:target], key=lambda x: x[0])


def _rank_sync_candidates(scored: list[tuple[float, float, float]], duration: float, bins: int) -> list[tuple[float, float, float]]:
    """Rank candidates with runtime coverage before pure score ordering."""
    if not scored:
        return []
    bins = max(1, int(bins))
    per_bin: dict[int, tuple[float, float, float]] = {}
    for item in scored:
        idx = min(bins - 1, int((item[0] / max(duration, 0.001)) * bins))
        if idx not in per_bin or item[1] > per_bin[idx][1]:
            per_bin[idx] = item
    ranked = sorted(per_bin.values(), key=lambda x: x[1], reverse=True)
    seen = {round(x[0], 3) for x in ranked}
    for item in sorted(scored, key=lambda x: x[1], reverse=True):
        if round(item[0], 3) not in seen:
            ranked.append(item); seen.add(round(item[0], 3))
    return ranked


def _normalized_failure_message(message: str) -> str:
    message = re.sub(r"media-manager-syncnet-[A-Za-z0-9_.-]+", "media-manager-syncnet-<temp>", message)
    message = re.sub(r"clip-\d{3}", "clip-<n>", message)
    return message


def _failure_signature(record: dict[str, Any]) -> str:
    payload = {
        "classification": str(record.get("classification") or "UNKNOWN"),
        "phase": str(record.get("phase") or "unknown"),
        "exception_type": str(record.get("exception_type") or "Exception"),
        "message": _normalized_failure_message(str(record.get("error") or "unknown")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _catalog_failure(cp: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Store one traceback/environment sample and leave a compact candidate reference."""
    failure_id = _failure_signature(record)
    catalog = cp.setdefault("syncnet_failure_catalog", {})
    if failure_id not in catalog:
        environment = dict(record.get("environment") or {})
        catalog[failure_id] = {
            "classification": record.get("classification"),
            "phase": record.get("phase"),
            "exception_type": record.get("exception_type"),
            "message": record.get("error"),
            "normalized_message": _normalized_failure_message(str(record.get("error") or "unknown")),
            "traceback": record.get("traceback"),
            **environment,
        }
    compact = dict(record)
    compact["failure_id"] = failure_id
    compact.pop("traceback", None)
    compact.pop("environment", None)
    return compact


def _syncnet_diagnostics(
    sync_results: dict[str, dict[str, Any]],
    threshold: float,
    failure_catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    failure_catalog = failure_catalog or {}
    good = [r for r in sync_results.values() if r.get("ok") and r.get("confidence") is not None]
    confs = sorted(float(r["confidence"]) for r in good)
    high = [c for c in confs if c >= threshold]
    no_faces = [
        r for r in sync_results.values()
        if not r.get("ok") and r.get("classification") == AVSyncClassification.SYNCNET_NO_FACE.value
    ]
    failures = [r for r in sync_results.values() if not r.get("ok") and r not in no_faces]
    diag: dict[str, Any] = {
        "tested": len(sync_results),
        "successful": len(good),
        "valid_results": len(good) + len(no_faces),
        "failed": len(failures),
        "no_face": len(no_faces),
        "low_confidence": sum(1 for c in confs if c < threshold),
        "confidence_threshold": threshold,
        "high_confidence": len(high),
        "best_confidence": max(confs) if confs else None,
        "median_confidence": statistics.median(confs) if confs else None,
    }
    if confs:
        pos = max(0, min(len(confs)-1, int(math.ceil(0.9 * len(confs))) - 1))
        diag["p90_confidence"] = confs[pos]
    if failures:
        counts: dict[str, int] = {}
        groups: dict[str, dict[str, Any]] = {}
        classification_counts: dict[str, int] = {}
        for r in failures:
            evidence = dict(failure_catalog.get(str(r.get("failure_id") or "")) or {})
            merged = {**evidence, **r}
            msg = str(merged.get("error") or merged.get("message") or "unknown")
            counts[msg] = counts.get(msg, 0) + 1
            classification = str(merged.get("classification") or "UNKNOWN")
            classification_counts[classification] = classification_counts.get(classification, 0) + 1
            group_id = str(r.get("failure_id") or _failure_signature(merged))
            if group_id not in groups:
                groups[group_id] = {
                    "failure_id": group_id,
                    "count": 0,
                    "classification": classification,
                    "phase": merged.get("phase"),
                    "exception_type": merged.get("exception_type"),
                    "message": msg,
                    "traceback": merged.get("traceback"),
                    "syncnet_version": merged.get("syncnet_version"),
                    "syncnet_module_path": merged.get("syncnet_module_path"),
                    "python_version": merged.get("python_version"),
                    "torch_version": merged.get("torch_version"),
                    "scenedetect_version": merged.get("scenedetect_version"),
                    "device": merged.get("device"),
                }
            groups[group_id]["count"] += 1
        diag["failure_reasons"] = counts
        diag["failure_classifications"] = classification_counts
        diag["failure_groups"] = list(groups.values())
    if not good and failures and not no_faces:
        kinds = {str(r.get("classification") or "") for r in failures}
        diag["classification"] = next(iter(kinds)) if len(kinds) == 1 else "SYNCNET_BACKEND_FAILURE"
    elif good and not high:
        diag["classification"] = AVSyncClassification.SYNCNET_LOW_CONFIDENCE.value
    elif not good and no_faces:
        diag["classification"] = AVSyncClassification.SYNCNET_NO_FACE.value
    elif len(high) < 3:
        diag["classification"] = AVSyncClassification.INSUFFICIENT_EVIDENCE.value
    return diag


def _syncnet_progress_message(diag: dict[str, Any], threshold: float) -> str:
    best = diag.get("best_confidence")
    best_text = "none" if best is None else f"{float(best):.2f}"
    return (
        f"strong={int(diag.get('high_confidence', 0) or 0)}/3 "
        f"usable={int(diag.get('successful', 0) or 0)} "
        f"no-face={int(diag.get('no_face', 0) or 0)} "
        f"errors={int(diag.get('failed', 0) or 0)} "
        f"best={best_text} threshold={threshold:g}"
    )

def _face_window_score(path: Path, start: float, seconds: float, fps: float = 2.0) -> float | None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    ffmpeg = require_tool("ffmpeg")
    width = 640
    # Probe height through source ratio using ffprobe is avoided here; image2pipe JPEG keeps dimensions.
    proc = run([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(path),
        "-t", f"{seconds:.3f}", "-vf", f"fps={max(0.1, float(fps)):g},scale={width}:-2", "-an", "-sn",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-",
    ], text=False)
    data = proc.stdout or b""
    # Split JPEG stream at SOI/EOI markers.
    frames = []
    pos = 0
    while True:
        a = data.find(b"\xff\xd8", pos)
        if a < 0:
            break
        b = data.find(b"\xff\xd9", a + 2)
        if b < 0:
            break
        arr = np.frombuffer(data[a:b+2], dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if frame is not None:
            frames.append(frame)
        pos = b + 2
    if not frames:
        return 0.0
    frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    face_frames = 0
    size_scores: list[float] = []
    mouth_motion: list[float] = []
    prev_mouth = None
    for frame in frames:
        faces = list(frontal.detectMultiScale(frame, scaleFactor=1.10, minNeighbors=4, minSize=(28, 28)))
        if not faces:
            faces = list(profile.detectMultiScale(frame, scaleFactor=1.10, minNeighbors=4, minSize=(28, 28)))
        if len(faces) == 0:
            prev_mouth = None
            continue
        x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
        face_frames += 1
        size_scores.append(min(1.0, (w * h) / (frame.shape[0] * frame.shape[1]) / 0.10))
        mouth = frame[y + int(h * 0.55): y + h, x:x+w]
        if mouth.size and prev_mouth is not None and prev_mouth.shape == mouth.shape:
            mouth_motion.append(float(cv2.absdiff(prev_mouth, mouth).mean()) / 32.0)
        prev_mouth = mouth.copy() if mouth.size else None
    persistence = face_frames / len(frames)
    size = statistics.median(size_scores) if size_scores else 0.0
    motion = min(1.0, statistics.median(mouth_motion)) if mouth_motion else 0.0
    return 0.55 * persistence + 0.25 * size + 0.20 * motion


def _parse_syncnet_result(result: Any) -> tuple[float, float, float | None] | None:
    if not isinstance(result, (tuple, list)) or len(result) != 7:
        raise SyncNetAdapterError(
            "Unexpected SyncNet result format: expected a seven-element tuple/list "
            "(offsets, confidences, distances, best_confidence, best_distance, detections, success)",
            phase="syncnet_result_parse",
            classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
        )

    offsets, confs, dists, best_conf, best_dist, _detections, success = result
    if not bool(success):
        return None

    def sequence(value: Any, label: str) -> list[Any]:
        if value is None or isinstance(value, (str, bytes, dict)):
            raise SyncNetAdapterError(
                f"Unexpected SyncNet result format: {label} must be a sequence",
                phase="syncnet_result_parse",
                classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
            )
        try:
            return list(value)
        except (TypeError, ValueError) as exc:
            raise SyncNetAdapterError(
                f"Unexpected SyncNet result format: {label} must be a sequence",
                phase="syncnet_result_parse",
                classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
                cause=exc,
            ) from exc

    raw_offsets = sequence(offsets, "offsets")
    raw_confs = sequence(confs, "confidences")
    raw_dists = sequence(dists, "distances")
    if not raw_offsets:
        raise SyncNetAdapterError(
            "Unexpected SyncNet result format: success=True but offsets is empty",
            phase="syncnet_result_parse",
            classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
        )
    if len(raw_offsets) != len(raw_confs):
        raise SyncNetAdapterError(
            f"Unexpected SyncNet result format: {len(raw_offsets)} offsets but {len(raw_confs)} confidences",
            phase="syncnet_result_parse",
            classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
        )
    if raw_dists and len(raw_dists) != len(raw_offsets):
        raise SyncNetAdapterError(
            f"Unexpected SyncNet result format: {len(raw_offsets)} offsets but {len(raw_dists)} distances",
            phase="syncnet_result_parse",
            classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
        )

    def finite_float(value: Any, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise SyncNetAdapterError(
                f"Unexpected SyncNet result format: {label} is not numeric",
                phase="syncnet_result_parse",
                classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
                cause=exc,
            ) from exc
        if not math.isfinite(parsed):
            raise SyncNetAdapterError(
                f"Unexpected SyncNet result format: {label} is not finite",
                phase="syncnet_result_parse",
                classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
            )
        return parsed

    parsed_offsets = [finite_float(value, f"offsets[{i}]") for i, value in enumerate(raw_offsets)]
    parsed_confs = [finite_float(value, f"confidences[{i}]") for i, value in enumerate(raw_confs)]
    parsed_dists = [finite_float(value, f"distances[{i}]") for i, value in enumerate(raw_dists)]
    finite_float(best_conf, "best_confidence")
    parsed_best_dist = finite_float(best_dist, "best_distance")
    idx = max(range(len(parsed_offsets)), key=lambda i: parsed_confs[i])
    return parsed_offsets[idx], parsed_confs[idx], parsed_dists[idx] if parsed_dists else parsed_best_dist


def _terminate_process_group(proc: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
    """Stop a subprocess and every descendant in its dedicated process group."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=max(0.0, grace_seconds))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        pass


def _bounded_ffmpeg_run(
    stream_spec,
    *positional,
    timeout_seconds: float,
    max_video_width: int | None = None,
    **options,
):
    """Run one ffmpeg-python graph with a deadline and process-tree cleanup.

    syncnet-python invokes ffmpeg-python without timeouts.  A wedged FFmpeg 7
    conversion can otherwise spin forever and also survive a parent-only pause.
    """
    import ffmpeg

    names = ["cmd", "capture_stdout", "capture_stderr", "input", "quiet", "overwrite_output"]
    if len(positional) > len(names):
        raise TypeError(f"run() takes at most {len(names) + 1} positional arguments")
    values: dict[str, Any] = {
        "cmd": "ffmpeg",
        "capture_stdout": False,
        "capture_stderr": False,
        "input": None,
        "quiet": False,
        "overwrite_output": False,
    }
    for name, value in zip(names, positional):
        values[name] = value
    for name in names:
        if name in options:
            if name in names[:len(positional)]:
                raise TypeError(f"run() got multiple values for argument '{name}'")
            values[name] = options.pop(name)
    if options:
        unexpected = next(iter(options))
        raise TypeError(f"run() got an unexpected keyword argument '{unexpected}'")

    args = list(ffmpeg.compile(
        stream_spec,
        cmd=values["cmd"],
        overwrite_output=bool(values["overwrite_output"]),
    ))
    # syncnet-python 0.2.2 still adds the removed/deprecated global `-async 1`
    # option to its intermediate AVI.  The pipeline supplies audio separately,
    # and the option has caused FFmpeg to spin after finishing the output file.
    cleaned: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token == "-async":
            skip_next = True
            continue
        cleaned.append(token)
    args = cleaned
    # The third-party pipeline otherwise decodes and runs S3FD over every 4K
    # frame even though its final face crops are only 224x224.  Bound just its
    # initial `video.avi` intermediate; audio is supplied independently from
    # the untouched candidate WAV, so the AVI does not need an audio stream.
    if max_video_width is not None and int(max_video_width) > 0:
        # `video.avi` also appears as the *input* of SyncNet's next graph when
        # frames are extracted. Only alter a matching output token; inserting
        # output options between `-i` and its filename invalidates that graph.
        video_output = next(
            (
                i for i, token in enumerate(args)
                if Path(str(token)).name == "video.avi"
                and (i == 0 or args[i - 1] != "-i")
            ),
            None,
        )
        if video_output is not None:
            width = int(max_video_width)
            args[video_output:video_output] = [
                "-an", "-vf", f"scale=w='min({width},iw)':h=-2",
            ]
    if "-nostdin" not in args:
        args.insert(1, "-nostdin")

    quiet = bool(values["quiet"])
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if values["input"] is not None else None,
        stdout=subprocess.PIPE if values["capture_stdout"] or quiet else None,
        stderr=subprocess.PIPE if values["capture_stderr"] or quiet else None,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(values["input"], timeout=max(0.1, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(proc)
        raise SyncNetAdapterError(
            f"SyncNet FFmpeg preprocessing exceeded {float(timeout_seconds):g} seconds",
            phase="syncnet_ffmpeg_timeout",
            classification=AVSyncClassification.CANDIDATE_PREPARATION_FAILED,
            cause=exc,
        ) from exc
    except BaseException:
        _terminate_process_group(proc)
        raise
    if proc.returncode:
        raise ffmpeg.Error("ffmpeg", out, err)
    return out, err


@contextlib.contextmanager
def _syncnet_candidate_deadline(seconds: float):
    """Bound a complete candidate evaluation when running on the main thread."""
    seconds = float(seconds)
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return
    old_handler = signal.getsignal(signal.SIGALRM)
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise SyncNetAdapterError(
            f"SyncNet candidate exceeded {seconds:g} seconds",
            phase="syncnet_candidate_timeout",
            classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
        )

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_delay > 0:
            remaining = max(1e-6, old_delay - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, old_interval)


@contextlib.contextmanager
def _quiet_syncnet_output(*, timeout_seconds: float = 180.0, max_video_width: int = 1920):
    """Contain third-party output and bound every FFmpeg subprocess it starts."""
    output_stream_type = None
    original_ffmpeg_run = None
    try:
        from ffmpeg.nodes import OutputStream

        output_stream_type = OutputStream
        original_ffmpeg_run = OutputStream.run

        def quiet_ffmpeg_run(stream, *args, **kwargs):
            kwargs.setdefault("quiet", True)
            return _bounded_ffmpeg_run(
                stream,
                *args,
                timeout_seconds=timeout_seconds,
                max_video_width=max_video_width,
                **kwargs,
            )

        OutputStream.run = quiet_ffmpeg_run
    except ImportError:
        # SyncNet itself will report the missing dependency.  Keep stdout
        # containment useful so the adapter can preserve the original error.
        pass

    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            yield
    finally:
        logging.disable(previous_logging_disable)
        if output_stream_type is not None and original_ffmpeg_run is not None:
            output_stream_type.run = original_ffmpeg_run


class SyncNetBackend:
    def __init__(self, root: Path, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.pipeline = None
        self._module = None
        self._prepare_error: SyncNetAdapterError | None = None

    @staticmethod
    def _version(distribution: str) -> str | None:
        try:
            return importlib.metadata.version(distribution)
        except Exception:
            return None

    def environment(self) -> dict[str, Any]:
        module_path = getattr(self._module, "__file__", None)
        if module_path is None:
            try:
                spec = importlib.util.find_spec("syncnet_python")
                module_path = spec.origin if spec else None
            except Exception:
                module_path = None
        return {
            "syncnet_version": self._version("syncnet-python"),
            "syncnet_module_path": str(module_path) if module_path else None,
            "python_version": platform.python_version(),
            "torch_version": self._version("torch"),
            "scenedetect_version": self._version("scenedetect"),
            "device": str(self.cfg.get("syncnet_device", "cpu")),
        }

    def available(self) -> tuple[bool, str]:
        try:
            self.prepare()
        except SyncNetAdapterError as exc:
            return False, str(exc)
        return True, "ok"

    def _path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.root / p

    def prepare(self):
        if self.pipeline is not None:
            return self.pipeline
        if self._prepare_error is not None:
            raise self._prepare_error

        try:
            try:
                self._module = importlib.import_module("syncnet_python")
            except Exception as exc:
                raise SyncNetAdapterError(
                    f"syncnet-python could not be imported: {type(exc).__name__}: {exc}",
                    phase="syncnet_import",
                    classification=AVSyncClassification.BACKEND_UNAVAILABLE,
                    cause=exc,
                ) from exc

            pipeline_class = getattr(self._module, "SyncNetPipeline", None)
            if not callable(pipeline_class):
                try:
                    direct = importlib.import_module("syncnet_python.syncnet_pipeline")
                    pipeline_class = getattr(direct, "SyncNetPipeline", None)
                except Exception as exc:
                    hint = ""
                    if isinstance(exc, ModuleNotFoundError) and exc.name == "scenedetect.video_manager":
                        hint = (
                            "; syncnet-python requires the legacy PySceneDetect VideoManager API "
                            "(install 'scenedetect[opencv]>=0.6,<0.7')"
                        )
                    raise SyncNetAdapterError(
                        "syncnet-python exported a non-callable SyncNetPipeline because its internal "
                        f"import failed: {type(exc).__name__}: {exc}{hint}",
                        phase="syncnet_import",
                        classification=AVSyncClassification.BACKEND_UNAVAILABLE,
                        cause=exc,
                    ) from exc
            if not callable(pipeline_class):
                raise SyncNetAdapterError(
                    "syncnet-python exported a non-callable SyncNetPipeline",
                    phase="syncnet_import",
                    classification=AVSyncClassification.BACKEND_UNAVAILABLE,
                )

            paths = {
                "S3FD": self._path(self.cfg["syncnet_s3fd_weights"]),
                "SyncNet": self._path(self.cfg["syncnet_weights"]),
            }
            for label, path in paths.items():
                try:
                    valid = path.is_file() and path.stat().st_size > 0
                    if valid:
                        with path.open("rb") as stream:
                            valid = bool(stream.read(1))
                except OSError:
                    valid = False
                if not valid:
                    raise SyncNetAdapterError(
                        f"{label} weight file is missing, empty, or unreadable: {path}",
                        phase="syncnet_pipeline_init",
                        classification=AVSyncClassification.BACKEND_UNAVAILABLE,
                    )

            try:
                pipeline = pipeline_class(
                    s3fd_weights=str(paths["S3FD"]),
                    syncnet_weights=str(paths["SyncNet"]),
                    device=str(self.cfg.get("syncnet_device", "cpu")),
                )
            except Exception as exc:
                raise SyncNetAdapterError(
                    f"SyncNet pipeline initialization failed: {type(exc).__name__}: {exc}",
                    phase="syncnet_pipeline_init",
                    classification=AVSyncClassification.BACKEND_INITIALIZATION_FAILED,
                    cause=exc,
                ) from exc
            if pipeline is None or not callable(getattr(pipeline, "inference", None)):
                raise SyncNetAdapterError(
                    "SyncNet pipeline initialized without a callable inference method",
                    phase="syncnet_pipeline_init",
                    classification=AVSyncClassification.BACKEND_INITIALIZATION_FAILED,
                )
            self.pipeline = pipeline
            return self.pipeline
        except SyncNetAdapterError as exc:
            self._prepare_error = exc
            raise

    def _get(self):
        return self.prepare()

    def evaluate(self, clip: Path) -> tuple[float, float, float | None] | None:
        # syncnet-python's public README documents ``audio_path=None`` as
        # extracting audio from the video, but current releases actually call
        # ffmpeg.input(audio_path) unconditionally.  Passing None therefore
        # fails inside the third-party package before any SyncNet evaluation.
        # Extract the already-selected authoritative audio from our candidate
        # clip and pass an explicit 16 kHz mono WAV instead.
        pipeline = self.prepare()
        ffmpeg_timeout = float(self.cfg.get("syncnet_ffmpeg_timeout_seconds", 180.0))
        candidate_timeout = float(self.cfg.get("syncnet_candidate_timeout_seconds", 900.0))
        preprocess_max_width = int(self.cfg.get("syncnet_preprocess_max_width", 1920))
        with _syncnet_candidate_deadline(candidate_timeout):
            with tempfile.TemporaryDirectory(prefix="media-manager-syncnet-") as td:
                td_path = Path(td)
                audio = td_path / "candidate.wav"
                try:
                    ffmpeg = require_tool("ffmpeg")
                    run([
                        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(clip), "-map", "0:a:0", "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
                    ], timeout=ffmpeg_timeout)
                    if not audio.is_file() or audio.stat().st_size <= 44:
                        raise MediaManagerError("extracted WAV is missing or empty")
                except SyncNetAdapterError:
                    raise
                except Exception as exc:
                    raise SyncNetAdapterError(
                        f"Could not extract candidate audio for SyncNet: {type(exc).__name__}: {exc}",
                        phase="candidate_audio_extract",
                        classification=AVSyncClassification.CANDIDATE_PREPARATION_FAILED,
                        cause=exc,
                    ) from exc
                try:
                    with _quiet_syncnet_output(
                        timeout_seconds=ffmpeg_timeout,
                        max_video_width=preprocess_max_width,
                    ):
                        result = pipeline.inference(
                            video_path=str(clip),
                            audio_path=str(audio),
                            cache_dir=str(td_path / "cache"),
                        )
                except SyncNetAdapterError:
                    raise
                except Exception as exc:
                    raise SyncNetAdapterError(
                        f"SyncNet inference failed: {type(exc).__name__}: {exc}",
                        phase="syncnet_inference",
                        classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
                        cause=exc,
                    ) from exc
        return _parse_syncnet_result(result)


def _make_analysis_clip(path: Path, start: float, seconds: float, audio_index: int, output: Path) -> None:
    ffmpeg = require_tool("ffmpeg")
    if output.exists():
        output.unlink()
    # Stream copy preserves the source A/V timing relationship. Fast seek may begin
    # slightly before the requested timestamp, which is harmless for offset analysis.
    run([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
        "-i", str(path), "-t", f"{seconds:.3f}", "-map", "0:v:0", "-map", f"0:a:{audio_index}",
        "-c", "copy", str(output),
    ])


def _linear_drift(observations: list[AVObservation]) -> float:
    if len(observations) < 3:
        return 0.0
    xs = [x.time for x in observations]
    ys = [x.offset_frames * 40.0 for x in observations]  # SyncNet convention is 25 fps.
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - xm) ** 2 for x in xs)
    if denom <= 0:
        return 0.0
    slope_ms_per_sec = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / denom
    return slope_ms_per_sec * 3600.0


def _av_observation_from_dict(data: dict[str, Any]) -> AVObservation:
    return AVObservation(
        time=float(data["time"]),
        offset_frames=float(data["offset_frames"]),
        confidence=float(data["confidence"]),
        min_distance=float(data["min_distance"]) if data.get("min_distance") is not None else None,
        face_score=float(data["face_score"]) if data.get("face_score") is not None else None,
    )


def _av_analysis_from_dict(data: dict[str, Any]) -> AVAnalysis:
    return AVAnalysis(
        status=StageStatus(str(data["status"])),
        observations=[_av_observation_from_dict(x) for x in (data.get("observations") or [])],
        median_offset_frames=float(data["median_offset_frames"]) if data.get("median_offset_frames") is not None else None,
        median_offset_ms=float(data["median_offset_ms"]) if data.get("median_offset_ms") is not None else None,
        spread_ms=float(data["spread_ms"]) if data.get("spread_ms") is not None else None,
        drift_ms_per_hour=float(data["drift_ms_per_hour"]) if data.get("drift_ms_per_hour") is not None else None,
        confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
        reason=data.get("reason"),
        reference_audio_index=int(data["reference_audio_index"]) if data.get("reference_audio_index") is not None else None,
        diagnostics=dict(data.get("diagnostics") or {}),
        classification=AVSyncClassification(str(data["classification"])) if data.get("classification") else None,
    )


def _syncnet_exception_record(
    exc: BaseException,
    backend: SyncNetBackend,
    *,
    time: float | None = None,
    phase: str | None = None,
    classification: AVSyncClassification | None = None,
) -> dict[str, Any]:
    resolved_classification = classification or getattr(
        exc, "classification", AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION
    )
    record: dict[str, Any] = {
        "ok": False,
        "error": str(exc),
        "exception_type": getattr(exc, "exception_type", type(exc).__name__),
        "phase": phase or getattr(exc, "phase", "syncnet_inference"),
        "classification": str(resolved_classification),
        "traceback": traceback.format_exc(),
        "environment": backend.environment(),
    }
    if time is not None:
        record["time"] = time
    return record


def analyze_av_sync(
    path: Path,
    probe: ProbeInfo,
    root: Path,
    cfg: dict,
    visual_type: VisualType,
    *,
    checkpoint_store: CheckpointStore | None = None,
    work_dir: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> AVAnalysis:
    audio_index, audio_reason = choose_authoritative_audio(probe, str(cfg["authoritative_language"]))
    if audio_index is None:
        return AVAnalysis(
            StageStatus.NEEDS_REVIEW,
            reason=audio_reason,
            classification=AVSyncClassification.INSUFFICIENT_EVIDENCE,
        )
    # Visual type is informational, not a hard gate. SyncNet's own S3FD +
    # audio/visual confidence is the actual authority for whether usable
    # speaking-face evidence exists. Animation/mixed content may still be
    # analyzed; automatic correction policy is enforced by the executor.
    sig = source_signature(path)
    cp: dict[str, Any] = checkpoint_store.load("av_sync") if checkpoint_store else {}
    cfg_key = json.dumps({
        "algorithm": AVSYNC_ALGORITHM_VERSION,
        "authoritative_language": cfg.get("authoritative_language"),
        "candidate_seconds": cfg.get("candidate_seconds"),
        "candidate_count": cfg.get("candidate_count"),
        "candidate_max_count": cfg.get("candidate_max_count"),
        "face_candidate_count": cfg.get("face_candidate_count"),
        "face_score_fps": cfg.get("face_score_fps"),
        "runtime_bins": cfg.get("runtime_bins"),
        "syncnet_preprocess_max_width": cfg.get("syncnet_preprocess_max_width"),
        "min_confidence": cfg.get("min_confidence"),
        "not_needed_threshold_ms": cfg.get("not_needed_threshold_ms"),
        "max_auto_offset_ms": cfg.get("max_auto_offset_ms"),
        "max_spread_ms": cfg.get("max_spread_ms"),
        "max_drift_ms_per_hour": cfg.get("max_drift_ms_per_hour"),
        "visual_type": visual_type.value,
    }, sort_keys=True)
    if cp.get("source_signature") != sig or cp.get("config_key") != cfg_key:
        prior = cp if cp.get("source_signature") == sig else {}
        cp = {
            "version": 4,
            "source_signature": sig,
            "config_key": cfg_key,
            "face_scores": {},
            "syncnet_results": {},
            "syncnet_failure_catalog": {},
        }
        # VAD output is independent of the SyncNet candidate-ranking policy.
        # Preserve it across algorithm upgrades when the source file is identical.
        if prior.get("speech_segments"):
            cp["speech_segments"] = prior["speech_segments"]
            cp["speech_method"] = prior.get("speech_method", "checkpoint")
            if prior.get("speech_fallback_reason"):
                cp["speech_fallback_reason"] = prior["speech_fallback_reason"]
        if checkpoint_store:
            checkpoint_store.save("av_sync", cp)

    if isinstance(cp.get("final_analysis"), dict):
        return _av_analysis_from_dict(cp["final_analysis"])

    backend = SyncNetBackend(root, cfg)
    try:
        backend.prepare()
    except SyncNetAdapterError as exc:
        failure = _catalog_failure(cp, _syncnet_exception_record(exc, backend))
        diagnostics = _syncnet_diagnostics(
            {"backend_prepare": failure},
            float(cfg["min_confidence"]),
            cp.get("syncnet_failure_catalog"),
        )
        diagnostics["tested"] = 0
        diagnostics["initialization_failures"] = 1
        status = (
            StageStatus.UNAVAILABLE
            if exc.classification == AVSyncClassification.BACKEND_UNAVAILABLE
            else StageStatus.NEEDS_REVIEW
        )
        prefix = "SyncNet backend unavailable" if status == StageStatus.UNAVAILABLE else "SyncNet backend initialization failed"
        result = AVAnalysis(
            status,
            reason=f"{prefix}: {exc}",
            reference_audio_index=audio_index,
            diagnostics=diagnostics,
            classification=exc.classification,
        )
        cp["phase"] = exc.phase
        cp["final_analysis"] = {**asdict(result), "status": result.status.value}
        if checkpoint_store:
            checkpoint_store.save("av_sync", cp)
        return result

    if work_dir is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="media-manager-av-")
        work = Path(temp_ctx.name)
    else:
        temp_ctx = None
        work = work_dir / "av-analysis"
        work.mkdir(parents=True, exist_ok=True)

    try:
        if "speech_segments" not in cp:
            if progress:
                progress({"phase": "speech_scan", "message": "Finding sustained speech in authoritative audio"})
            speech_fallback_reason = None
            try:
                speech = _silero_speech(
                    path, audio_index, work, duration=probe.duration, progress=progress
                )
            except Exception as exc:
                speech = None
                speech_fallback_reason = str(exc)
                if progress:
                    progress({
                        "phase": "speech_scan",
                        "message": f"Silero unavailable; falling back to FFmpeg non-silence detection: {exc}",
                    })
            speech_method = "silero_ffmpeg_pcm" if speech is not None else "ffmpeg_nonsilence"
            if speech is None:
                speech = _ffmpeg_nonsilence(path, audio_index, probe.duration)
            cp["speech_segments"] = [[float(a), float(b)] for a, b in speech]
            cp["speech_method"] = speech_method
            if speech_fallback_reason:
                cp["speech_fallback_reason"] = speech_fallback_reason
            cp["phase"] = "speech_scan_complete"
            if checkpoint_store:
                checkpoint_store.save("av_sync", cp)
            if progress:
                progress({"phase": "speech_scan_complete", "segments": len(speech)})
        else:
            speech = [(float(a), float(b)) for a, b in cp.get("speech_segments", [])]
            speech_method = str(cp.get("speech_method") or "checkpoint")

        if "candidates" not in cp:
            candidates = _speech_candidates(speech, probe.duration, cfg)
            cp["candidates"] = [[float(a), float(b)] for a, b in candidates]
            cp["phase"] = "candidates_selected"
            if checkpoint_store:
                checkpoint_store.save("av_sync", cp)
            if progress:
                progress({"phase": "candidates_selected", "completed": 0, "total": len(candidates), "message": f"Scoring {len(candidates)} speech windows for visible faces/mouth motion"})
        else:
            candidates = [(float(a), float(b)) for a, b in cp.get("candidates", [])]

        if not candidates:
            result = AVAnalysis(
                StageStatus.INSUFFICIENT_EVIDENCE,
                reason="No sustained speech candidates",
                reference_audio_index=audio_index,
                classification=AVSyncClassification.INSUFFICIENT_EVIDENCE,
            )
            cp["final_analysis"] = {**asdict(result), "status": result.status.value}
            if checkpoint_store:
                checkpoint_store.save("av_sync", cp)
            return result

        face_scores = cp.setdefault("face_scores", {})
        scored: list[tuple[float, float, float]] = []
        for i, (start_t, speech_score) in enumerate(candidates):
            key = f"{start_t:.3f}"
            if key not in face_scores:
                face_score = _face_window_score(
                    path,
                    start_t,
                    float(cfg["candidate_seconds"]),
                    float(cfg.get("face_score_fps", 2.0)),
                )
                face_scores[key] = None if face_score is None else float(face_score)
                cp["phase"] = "face_scoring"
                if checkpoint_store:
                    checkpoint_store.save("av_sync", cp)
            raw_face = face_scores.get(key)
            face_score = float(raw_face) if raw_face is not None else None
            combined = 0.65 * speech_score + 0.35 * (face_score if face_score is not None else 0.5)
            scored.append((start_t, combined, face_score if face_score is not None else -1.0))
            if progress:
                progress({"phase": "face_scoring", "completed": i + 1, "total": len(candidates)})

        ranked = _rank_sync_candidates(scored, probe.duration, int(cfg.get("runtime_bins", 24)))
        # Eight distributed candidates are sufficient for the initial pass.
        # Difficult media still expands adaptively until it finds three strong
        # observations or reaches candidate_max_count.
        initial_target = max(3, int(cfg.get("candidate_count", 8)))
        max_target = max(initial_target, int(cfg.get("candidate_max_count", 64)))
        max_target = min(max_target, len(ranked))
        cp["ranked_candidates"] = [[float(a), float(b), float(c)] for a, b, c in ranked[:max_target]]
        sync_results = cp.setdefault("syncnet_results", {})
        threshold = float(cfg["min_confidence"])

        # Always perform the initial distributed pass. If it yields fewer than
        # three strong observations, progressively widen the search rather than
        # giving up on the first fixed set of clips.
        tested_this_policy = 0
        consecutive_failure_id: str | None = None
        consecutive_failures = 0
        circuit_breaker: dict[str, Any] | None = None
        for i, (start_t, _score, face_score) in enumerate(ranked[:max_target]):
            key = f"{start_t:.3f}"
            if key not in sync_results:
                clip = work / f"clip-{i:03d}.mkv"
                record: dict[str, Any]
                if progress:
                    diag_before = _syncnet_diagnostics(
                        sync_results, threshold, cp.get("syncnet_failure_catalog")
                    )
                    progress({
                        "phase": "syncnet",
                        "completed": tested_this_policy,
                        "total": max_target,
                        "current_candidate": i + 1,
                        "candidate_phase": "clip_extract",
                        "message": f"candidate {i + 1} preparing; " + _syncnet_progress_message(diag_before, threshold),
                    })
                try:
                    _make_analysis_clip(path, start_t, float(cfg["candidate_seconds"]), audio_index, clip)
                except Exception as exc:
                    record = _catalog_failure(cp, _syncnet_exception_record(
                        exc,
                        backend,
                        time=start_t,
                        phase="candidate_clip_extract",
                        classification=AVSyncClassification.CANDIDATE_PREPARATION_FAILED,
                    ))
                else:
                    try:
                        if progress:
                            progress({
                                "phase": "syncnet",
                                "completed": tested_this_policy,
                                "total": max_target,
                                "current_candidate": i + 1,
                                "candidate_phase": "inference",
                                "message": f"candidate {i + 1} inference; " + _syncnet_progress_message(diag_before, threshold),
                            })
                        evaluation = backend.evaluate(clip)
                    except Exception as exc:
                        record = _catalog_failure(cp, _syncnet_exception_record(exc, backend, time=start_t))
                    else:
                        if evaluation is None:
                            record = {
                                "ok": False,
                                "valid_result": True,
                                "time": start_t,
                                "error": "SyncNet found no usable speaking-face track",
                                "phase": "syncnet_result_parse",
                                "classification": AVSyncClassification.SYNCNET_NO_FACE.value,
                            }
                        else:
                            offset, conf, dist = evaluation
                            record = {
                                "ok": True,
                                "time": start_t,
                                "offset_frames": offset,
                                "confidence": conf,
                                "min_distance": dist,
                                "face_score": None if face_score < 0 else face_score,
                            }
                sync_results[key] = record
                cp["phase"] = "syncnet"
                if checkpoint_store:
                    checkpoint_store.save("av_sync", cp)
            tested_this_policy += 1
            record = sync_results[key]
            structural_failure = (
                not record.get("ok")
                and (
                    (
                        record.get("classification") == AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION.value
                        and record.get("phase") in {
                            "syncnet_inference", "syncnet_result_parse", "syncnet_candidate_timeout",
                        }
                    )
                    or record.get("phase") == "syncnet_ffmpeg_timeout"
                )
            )
            if structural_failure:
                failure_id = str(record.get("failure_id") or _failure_signature(record))
                if failure_id == consecutive_failure_id:
                    consecutive_failures += 1
                else:
                    consecutive_failure_id = failure_id
                    consecutive_failures = 1
            else:
                consecutive_failure_id = None
                consecutive_failures = 0

            if consecutive_failures >= 3:
                evidence = dict((cp.get("syncnet_failure_catalog") or {}).get(consecutive_failure_id) or {})
                circuit_breaker = {
                    "triggered": True,
                    "threshold": 3,
                    "consecutive": consecutive_failures,
                    "failure_id": consecutive_failure_id,
                    "classification": record.get("classification"),
                    "phase": record.get("phase"),
                    "exception_type": evidence.get("exception_type") or record.get("exception_type"),
                    "message": evidence.get("message") or record.get("error"),
                    "skipped_candidates": max(0, max_target - tested_this_policy),
                }
                cp["syncnet_circuit_breaker"] = circuit_breaker

            diag_now = _syncnet_diagnostics(
                sync_results, threshold, cp.get("syncnet_failure_catalog")
            )
            if circuit_breaker:
                diag_now["circuit_breaker"] = circuit_breaker
            # Don't stop before the initial pass has been covered. Afterwards,
            # three high-confidence clips are sufficient to proceed to robust
            # cross-runtime consistency checks.
            if progress:
                phase_msg = _syncnet_progress_message(diag_now, threshold)
                if circuit_breaker:
                    phase_msg = (
                        f"SyncNet backend failure: {consecutive_failures}/3 candidates failed identically; "
                        "adaptive search stopped"
                    )
                progress({
                    "phase": "syncnet",
                    "completed": min(tested_this_policy, max_target),
                    "total": max_target,
                    "high_confidence": diag_now["high_confidence"],
                    "best_confidence": diag_now.get("best_confidence"),
                    "message": phase_msg,
                    "checkpoint_path": str(checkpoint_store.path_for("av_sync")) if checkpoint_store else None,
                })
            if circuit_breaker:
                if checkpoint_store:
                    checkpoint_store.save("av_sync", cp)
                break
            if tested_this_policy >= initial_target and diag_now["high_confidence"] >= 3:
                break

        cp["syncnet_diagnostics"] = _syncnet_diagnostics(
            sync_results, threshold, cp.get("syncnet_failure_catalog")
        )
        if circuit_breaker:
            cp["syncnet_diagnostics"]["circuit_breaker"] = circuit_breaker
        cp["chosen"] = cp["ranked_candidates"][:tested_this_policy]
        if checkpoint_store:
            checkpoint_store.save("av_sync", cp)

        observations: list[AVObservation] = []
        for _key, record in sorted(sync_results.items()):
            if not record.get("ok"):
                continue
            conf = float(record["confidence"])
            if conf < float(cfg["min_confidence"]):
                continue
            observations.append(AVObservation(
                float(record["time"]), float(record["offset_frames"]), conf,
                float(record["min_distance"]) if record.get("min_distance") is not None else None,
                float(record["face_score"]) if record.get("face_score") is not None else None,
            ))
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()

    diagnostics = dict(cp.get("syncnet_diagnostics") or {})
    circuit_breaker = diagnostics.get("circuit_breaker") or {}
    if circuit_breaker.get("triggered"):
        successful = int(diagnostics.get("successful", 0) or 0)
        result = AVAnalysis(
            StageStatus.NEEDS_REVIEW,
            observations=observations,
            reason=(
                "SyncNet backend failure: "
                f"{circuit_breaker.get('consecutive', 3)}/{circuit_breaker.get('threshold', 3)} "
                f"consecutive candidates failed identically; {successful} valid confidence results; "
                f"stopped before {circuit_breaker.get('skipped_candidates', 0)} remaining candidates"
            ),
            reference_audio_index=audio_index,
            diagnostics=diagnostics,
            classification=AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION,
        )
        cp["final_analysis"] = {**asdict(result), "status": result.status.value}
        if checkpoint_store:
            checkpoint_store.save("av_sync", cp)
        return result

    if len(observations) < 3:
        best = diagnostics.get("best_confidence")
        tested = diagnostics.get("tested", 0)
        threshold = diagnostics.get("confidence_threshold", cfg.get("min_confidence"))
        best_text = f"; best confidence {float(best):.3f}" if best is not None else ""
        successful = int(diagnostics.get("successful", 0) or 0)
        failed = int(diagnostics.get("failed", 0) or 0)
        no_face = int(diagnostics.get("no_face", 0) or 0)
        if successful == 0 and failed > 0:
            failure_classes = diagnostics.get("failure_classifications") or {}
            candidate_only = set(failure_classes) == {AVSyncClassification.CANDIDATE_PREPARATION_FAILED.value}
            status = StageStatus.NEEDS_REVIEW
            classification = (
                AVSyncClassification.CANDIDATE_PREPARATION_FAILED
                if candidate_only
                else AVSyncClassification.SYNCNET_INFERENCE_EXCEPTION
            )
            label = "candidate preparation failure" if candidate_only else "backend failure"
            reason = (
                f"SyncNet {label}: 0 valid confidence results because {failed}/{tested} "
                f"candidates failed; no_face={no_face}"
            )
        elif successful == 0 and no_face > 0:
            status = StageStatus.INSUFFICIENT_EVIDENCE
            classification = AVSyncClassification.SYNCNET_NO_FACE
            reason = f"SyncNet returned no usable speaking-face track in {no_face} valid candidate evaluations"
        elif successful > 0 and int(diagnostics.get("high_confidence", 0) or 0) == 0:
            status = StageStatus.INSUFFICIENT_EVIDENCE
            classification = AVSyncClassification.SYNCNET_LOW_CONFIDENCE
            reason = (
                f"SyncNet returned {successful} valid confidence results, but none exceeded "
                f"the {threshold} confidence threshold{best_text}"
            )
        else:
            status = StageStatus.INSUFFICIENT_EVIDENCE
            classification = AVSyncClassification.INSUFFICIENT_EVIDENCE
            reason = (
                f"Only {len(observations)} high-confidence SyncNet observations after {tested} "
                f"candidates (threshold {threshold}){best_text}"
            )
        result = AVAnalysis(
            status,
            observations=observations,
            reason=reason,
            reference_audio_index=audio_index,
            diagnostics=diagnostics,
            classification=classification,
        )
        cp["final_analysis"] = {**asdict(result), "status": result.status.value}
        if checkpoint_store:
            checkpoint_store.save("av_sync", cp)
        return result

    offsets_ms = [o.offset_frames * 40.0 for o in observations]
    median_ms = statistics.median(offsets_ms)
    median_frames = statistics.median(o.offset_frames for o in observations)
    mad = statistics.median(abs(x - median_ms) for x in offsets_ms)
    spread = mad * 1.4826
    drift = _linear_drift(observations)
    conf = statistics.median(o.confidence for o in observations)

    if abs(drift) > float(cfg["max_drift_ms_per_hour"]):
        status = StageStatus.NEEDS_REVIEW
        why = f"Progressive drift detected ({drift:.1f} ms/hour)"
    elif spread > float(cfg["max_spread_ms"]):
        status = StageStatus.NEEDS_REVIEW
        why = f"SyncNet observations disagree (robust spread {spread:.1f} ms)"
    elif abs(median_ms) <= float(cfg["not_needed_threshold_ms"]):
        status = StageStatus.NOT_NEEDED
        why = "A/V offset is within configured tolerance"
    elif abs(median_ms) > float(cfg["max_auto_offset_ms"]):
        status = StageStatus.NEEDS_REVIEW
        why = f"Offset {median_ms:.1f} ms exceeds automatic correction limit"
    else:
        status = StageStatus.COMPLETE
        why = "Consistent high-confidence global A/V offset"

    result = AVAnalysis(
        status=status,
        observations=observations,
        median_offset_frames=median_frames,
        median_offset_ms=median_ms,
        spread_ms=spread,
        drift_ms_per_hour=drift,
        confidence=conf,
        reason=why + f"; speech selector={speech_method}; visual_type={visual_type.value}; face selector is a hint, SyncNet/S3FD is authoritative",
        reference_audio_index=audio_index,
        diagnostics=diagnostics,
    )
    cp["phase"] = "analysis_complete"
    cp["final_analysis"] = {**asdict(result), "status": result.status.value}
    if checkpoint_store:
        checkpoint_store.save("av_sync", cp)
    return result


def _shift_audio_copy(input_path: Path, output_path: Path, shift_seconds: float) -> None:
    ffmpeg = require_tool("ffmpeg")
    if output_path.exists():
        output_path.unlink()
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(input_path), "-itsoffset", f"{shift_seconds:.6f}", "-i", str(input_path),
        "-map", "0:v?", "-map", "1:a?", "-map", "0:s?", "-map", "0:d?", "-map", "0:t?",
        "-map_metadata", "0", "-map_chapters", "0", "-c", "copy", str(output_path),
    ])


def _verify_shift_sign(
    input_path: Path,
    analysis: AVAnalysis,
    probe: ProbeInfo,
    root: Path,
    cfg: dict,
    work: Path,
) -> float:
    if not analysis.observations or analysis.median_offset_ms is None or analysis.reference_audio_index is None:
        raise MediaManagerError("Insufficient A/V analysis for correction")
    backend = SyncNetBackend(root, cfg)
    best_obs = max(analysis.observations, key=lambda o: o.confidence)
    source_clip = work / "sign-source.mkv"
    _make_analysis_clip(input_path, best_obs.time, float(cfg["candidate_seconds"]), analysis.reference_audio_index, source_clip)
    magnitude = abs(analysis.median_offset_ms) / 1000.0
    trials: list[tuple[float, float, float]] = []
    for shift in (-magnitude, magnitude):
        shifted = work / ("sign-neg.mkv" if shift < 0 else "sign-pos.mkv")
        _shift_audio_copy(source_clip, shifted, shift)
        try:
            evaluation = backend.evaluate(shifted)
            if evaluation is None:
                continue
            off, conf, _ = evaluation
            trials.append((shift, abs(off * 40.0), conf))
        except Exception:
            continue
    if not trials:
        raise MediaManagerError("Could not validate either A/V correction direction")
    shift, residual, conf = min(trials, key=lambda x: x[1])
    original_abs = abs(best_obs.offset_frames * 40.0)
    if residual >= original_abs * 0.65 or conf < float(cfg["min_confidence"]):
        raise MediaManagerError(
            f"Proposed A/V correction did not validate (original {original_abs:.1f} ms, residual {residual:.1f} ms)"
        )
    return shift


def apply_verified_av_correction(
    input_path: Path,
    output_path: Path,
    analysis: AVAnalysis,
    probe: ProbeInfo,
    root: Path,
    cfg: dict,
) -> dict[str, Any]:
    if analysis.status != StageStatus.COMPLETE or analysis.median_offset_ms is None:
        raise MediaManagerError("A/V analysis does not recommend an automatic correction")
    if len(probe.audio_streams) > 1 and not bool(cfg.get("shift_all_audio_streams", False)):
        raise MediaManagerError(
            "Multiple audio streams present; set av_sync.shift_all_audio_streams=true or review manually"
        )
    with tempfile.TemporaryDirectory(prefix="media-manager-avsign-") as td:
        shift = _verify_shift_sign(input_path, analysis, probe, root, cfg, Path(td))
    _shift_audio_copy(input_path, output_path, shift)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise MediaManagerError("A/V corrected output was not created")
    return {"applied_shift_seconds": shift, "detected_offset_ms": analysis.median_offset_ms}
