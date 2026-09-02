from __future__ import annotations

import shutil
from pathlib import Path

from .models import ProbeInfo
from .utils import MediaManagerError


def ensure_work_space(root: Path, input_path: Path, multiplier: float = 1.10) -> None:
    usage = shutil.disk_usage(root)
    needed = int(input_path.stat().st_size * multiplier) + 64 * 1024 * 1024
    if usage.free < needed:
        raise MediaManagerError(
            f"Insufficient free space for safe transaction: need about {needed/1024**3:.2f} GiB, "
            f"have {usage.free/1024**3:.2f} GiB"
        )


def verify_remux(before: ProbeInfo, after: ProbeInfo, *, crop: tuple[int,int,int,int] | None = None) -> dict:
    if before.video_codec != after.video_codec:
        raise MediaManagerError(f"Video codec changed: {before.video_codec} -> {after.video_codec}")
    if len(before.audio_streams) != len(after.audio_streams):
        raise MediaManagerError("Audio stream count changed during transaction")
    if len(before.subtitle_streams) != len(after.subtitle_streams):
        raise MediaManagerError("Embedded subtitle stream count changed during transaction")
    if abs(before.duration - after.duration) > 2.0:
        raise MediaManagerError(
            f"Duration changed unexpectedly: {before.duration:.3f}s -> {after.duration:.3f}s"
        )
    if crop and before.width and before.height and after.width and after.height:
        l, r, t, b = crop
        expected_w = before.width - l - r
        expected_h = before.height - t - b
        # Decoder/container reporting can differ by a couple of chroma pixels.
        if abs(after.width - expected_w) > 4 or abs(after.height - expected_h) > 4:
            raise MediaManagerError(
                f"Crop dimensions do not match expectation: expected about {expected_w}x{expected_h}, "
                f"got {after.width}x{after.height}"
            )
    return {
        "duration_before": before.duration,
        "duration_after": after.duration,
        "video_codec": after.video_codec,
        "audio_streams": len(after.audio_streams),
        "subtitle_streams": len(after.subtitle_streams),
        "width": after.width,
        "height": after.height,
    }
