from __future__ import annotations

import os
import stat
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

from .utils import MediaManagerError


DEFAULT_CONFIG: dict[str, Any] = {
    "library": {
        "root": "",
        "movies_directory": "Movies",
        "tv_directory": "TV Shows",
        "extensions": [".mp4"],
    },
    "processing": {
        "jobs": 1,
        "verify_before_commit": True,
        "create_markers": True,
        "checkpoint_seconds": 60.0,
        "preserve_incomplete_work": True,
    },
    "metadata": {"region": "US"},
    "audio_languages": {"model": "tiny", "timeout_seconds": 300, "preferred_languages": ["en"]},
    "av_sync": {
        "enabled": True,
        "backend": "syncnet",
        "future_backend": "synchformer",
        "authoritative_language": "eng",
        "candidate_seconds": 10.0,
        "candidate_count": 8,
        "candidate_max_count": 64,
        "face_candidate_count": 48,
        "face_score_fps": 2.0,
        "runtime_bins": 24,
        "min_confidence": 4.5,
        "not_needed_threshold_ms": 45.0,
        "max_auto_offset_ms": 500.0,
        "max_spread_ms": 80.0,
        "max_drift_ms_per_hour": 200.0,
        "apply_corrections": True,
        "shift_all_audio_streams": False,
        "allow_unknown_visual_type": True,
        "attempt_animation_analysis": True,
        # Retained for compatibility with older config files. Visual type is
        # informational in v0.6+; SyncNet evidence and sign validation decide.
        "auto_correct_animation": True,
        "visual_sample_count": 96,
        "syncnet_device": "cpu",
        "syncnet_ffmpeg_timeout_seconds": 180.0,
        "syncnet_candidate_timeout_seconds": 900.0,
        "syncnet_preprocess_max_width": 1280,
        "syncnet_s3fd_weights": ".media-manager/models/sfd_face.pth",
        "syncnet_weights": ".media-manager/models/syncnet_v2.model",
    },
    "crop": {
        "enabled": True,
        # One native-resolution crop sample every five seconds.  The denser
        # transition pass remains separate and only runs around detected
        # aspect-ratio changes.
        "scan_fps": 0.2,
        "transition_scan_fps": 5.0,
        "cropdetect_limit": 24.0 / 255.0,
        "cropdetect_round": 2,
        "black_yavg": 22.0,
        "black_yhigh": 38.0,
        "fade_guard_seconds": 2.0,
        "fade_yavg": 55.0,
        "native_verify_frames_per_side": 4,
        "pixel_black_luma": 28,
        "pixel_black_fraction": 0.995,
        "pixel_max_stddev": 12.0,
        "state_min_persistence_seconds": 3.0,
        "state_min_recurrence_samples": 8,
        "state_jitter_tolerance_pixels": 2,
    },
    "subtitles": {
        "enabled": True,
        "languages": ["en"],
        "prefer_existing": True,
        "plain_srt_language": "en",
        "hearing_impaired": "exclude",
        "ffsubsync_min_score": 0.0,
        "max_abs_offset_seconds": 600.0,
        "min_scale": 0.90,
        "max_scale": 1.10,
        # Full-runtime ffsubsync is primary. This flag permits multi-segment
        # fallback only when the primary result is low-quality or implausible.
        "multi_segment_sync": True,
        "segment_count": 8,
        "large_offset_seconds": 15.0,
        "large_scale_delta": 0.015,
        "known_rate_ratio_tolerance": 0.003,
        "regional_activity_min_overlap": 0.25,
        "alternative_recovery": True,
        # Small out-of-order cues are sometimes intentional (for overlapping
        # dialogue/signs). Large jumps usually mean multiple releases were
        # concatenated and must be rejected before synchronization.
        "max_backward_jump_seconds": 10.0,
        # Includes the first provider result; rejected IDs/hashes are persisted
        # so later runs continue with a different release.
        "alternative_candidate_limit": 3,
        "retry_cooldown_hours": 168.0,
        "provider_backoff_hours": 24.0,
        "alignment_timeout_seconds": 300.0,
        "attempt_budget_seconds": 900.0,
    },
    "opensubtitles": {
        "username": "",
        "api_key": "",
        "api_root": "https://api.opensubtitles.com/api/v1",
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"Unsupported TOML value: {type(v).__name__}")


def dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, dict):
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class LibraryConfig:
    def __init__(self, root: Path, data: dict[str, Any]):
        self.root = root.resolve()
        self.state_dir = self.root / ".media-manager"
        self.path = self.state_dir / "config.toml"
        self.opensubtitles_password_path = self.state_dir / "opensubtitles-password"
        self.data = data

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self.data[key]

    @classmethod
    def exists(cls, root: Path) -> bool:
        return (root.resolve() / ".media-manager" / "config.toml").exists()

    @classmethod
    def load(cls, root: Path) -> "LibraryConfig":
        root = root.resolve()
        path = root / ".media-manager" / "config.toml"
        if not path.exists():
            raise MediaManagerError(f"No media-manager config in {root}")
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        # Migrate the former shipped default; preserve non-default custom limits.
        av = raw.get("av_sync", {})
        if av.get("max_drift_ms_per_hour") == 80.0 and not av.get("drift_policy_version"):
            av["max_drift_ms_per_hour"] = 200.0
        av["drift_policy_version"] = 1
        cfg = cls(root, _merge(DEFAULT_CONFIG, raw))
        if cfg.data["library"].get("root") != str(root):
            cfg.data["library"]["root"] = str(root)
            cfg.save()
        return cfg

    @classmethod
    def init(
        cls,
        root: Path,
        *,
        os_user: str | None = None,
        os_api_key: str | None = None,
    ) -> "LibraryConfig":
        root = root.resolve()
        state = root / ".media-manager"
        state.mkdir(parents=True, exist_ok=True)
        for name in ("logs", "work", "models", "checkpoints"):
            (state / name).mkdir(exist_ok=True)
        data = deepcopy(DEFAULT_CONFIG)
        data["library"]["root"] = str(root)
        if os_user is not None:
            data["opensubtitles"]["username"] = os_user
        if os_api_key is not None:
            data["opensubtitles"]["api_key"] = os_api_key
        cfg = cls(root, data)
        cfg.save()
        return cfg

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(dump_toml(self.data), encoding="utf-8")
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def set_opensubtitles(self, username: str | None, api_key: str | None) -> None:
        if username is not None:
            self.data["opensubtitles"]["username"] = username
        if api_key is not None:
            self.data["opensubtitles"]["api_key"] = api_key
        self.save()

    def get_opensubtitles_password(self) -> str | None:
        path = self.opensubtitles_password_path
        if not path.exists():
            return None
        try:
            # Enforce the intended permissions even if the file was copied in
            # from another system with a more permissive mode.
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            password = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise MediaManagerError(f"Could not read OpenSubtitles password file: {path}") from exc
        return password or None

    def set_opensubtitles_password(self, password: str) -> None:
        if not password:
            raise MediaManagerError("OpenSubtitles password cannot be blank")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.opensubtitles_password_path
        tmp = path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(password + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)

    def resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def movies_root(self) -> Path:
        return self.root / self.data["library"]["movies_directory"]

    @property
    def tv_root(self) -> Path:
        return self.root / self.data["library"]["tv_directory"]
