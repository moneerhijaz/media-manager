from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class StageStatus(StrEnum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    NOT_NEEDED = "not_needed"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class AVSyncClassification(StrEnum):
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BACKEND_INITIALIZATION_FAILED = "BACKEND_INITIALIZATION_FAILED"
    CANDIDATE_PREPARATION_FAILED = "CANDIDATE_PREPARATION_FAILED"
    SYNCNET_NO_FACE = "SYNCNET_NO_FACE"
    SYNCNET_LOW_CONFIDENCE = "SYNCNET_LOW_CONFIDENCE"
    SYNCNET_INFERENCE_EXCEPTION = "SYNCNET_INFERENCE_EXCEPTION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


TERMINAL_OK = {
    StageStatus.COMPLETE,
    StageStatus.NOT_NEEDED,
    StageStatus.NOT_APPLICABLE,
    StageStatus.UNAVAILABLE,
}


class MediaType(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"


class VisualType(StrEnum):
    LIVE_ACTION = "live_action"
    ANIMATION = "animation"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class MediaIdentity:
    media_type: MediaType
    title: str
    year: int | None = None
    show_title: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    conflict: str | None = None


@dataclass(slots=True)
class ProbeInfo:
    duration: float
    format_name: str
    streams: list[dict[str, Any]]
    chapters: list[dict[str, Any]]
    video_codec: str | None
    width: int | None
    height: int | None
    audio_streams: list[dict[str, Any]]
    subtitle_streams: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CropSample:
    time: float
    left: int
    right: int
    top: int
    bottom: int
    yavg: float | None = None
    yhigh: float | None = None
    valid: bool = True

    @property
    def margins(self) -> tuple[int, int, int, int]:
        return self.left, self.right, self.top, self.bottom


@dataclass(slots=True)
class CropAnalysis:
    status: StageStatus
    variable: bool
    safe_left: int = 0
    safe_right: int = 0
    safe_top: int = 0
    safe_bottom: int = 0
    verified_left: int = 0
    verified_right: int = 0
    verified_top: int = 0
    verified_bottom: int = 0
    sample_count: int = 0
    ignored_dark: int = 0
    states: dict[str, int] = field(default_factory=dict)
    raw_states: dict[str, int] = field(default_factory=dict)
    normalized_states: dict[str, int] = field(default_factory=dict)
    meaningful_states: dict[str, int] = field(default_factory=dict)
    verification_times: list[float] = field(default_factory=list)
    method: str = "native_pixel_mask"
    algorithm_version: str | None = None
    crop_units: dict[str, int] = field(default_factory=dict)
    reason: str | None = None

    @property
    def has_crop(self) -> bool:
        return any((self.safe_left, self.safe_right, self.safe_top, self.safe_bottom))


@dataclass(slots=True)
class AVObservation:
    time: float
    offset_frames: float
    confidence: float
    min_distance: float | None = None
    face_score: float | None = None


@dataclass(slots=True)
class AVAnalysis:
    status: StageStatus
    observations: list[AVObservation] = field(default_factory=list)
    median_offset_frames: float | None = None
    median_offset_ms: float | None = None
    spread_ms: float | None = None
    drift_ms_per_hour: float | None = None
    confidence: float | None = None
    reason: str | None = None
    reference_audio_index: int | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    classification: AVSyncClassification | None = None


@dataclass(slots=True)
class SubtitleCandidate:
    file_id: int
    file_name: str
    language: str
    release: str
    score: float
    hash_match: bool
    trusted: bool
    hearing_impaired: bool
    title: str | None
    year: int | None
    feature_type: str | None
    season_number: int | None
    episode_number: int | None
    parent_title: str | None


@dataclass(slots=True)
class MediaRecord:
    id: int
    path: Path
    rel_path: str
    media_type: MediaType
    title: str
    year: int | None
    fingerprint: str
    size: int
    mtime_ns: int
    duration: float
    video_codec: str | None
    width: int | None
    height: int | None
    visual_type: VisualType
    overall_status: str
    show_title: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
