from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from media_manager.config import LibraryConfig
from media_manager.crop.engine import analyze_crop, apply_crop
from media_manager.probe import probe_media


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required",
)


def _make_segment(path: Path, *, top: int, bottom: int, color: str, seconds: float = 2.0):
    height = 360
    content_h = height - top - bottom
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=640x360:r=25:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-vf", f"drawbox=x=0:y={top}:w=640:h={content_h}:color={color}:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )


def test_variable_aspect_crops_only_common_unused_pixels(tmp_path: Path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    _make_segment(a, top=50, bottom=50, color="red")
    _make_segment(b, top=20, bottom=20, color="blue")
    listing = tmp_path / "list.txt"
    listing.write_text(f"file '{a}'\nfile '{b}'\n")
    src = tmp_path / "variable.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(src)],
        check=True,
    )
    cfg = LibraryConfig.init(tmp_path / "library")
    cfg.data["crop"]["scan_fps"] = 1.0
    probe = probe_media(src)
    analysis = analyze_crop(src, probe, cfg["crop"])
    assert analysis.variable
    assert analysis.safe_top == 20
    assert analysis.safe_bottom == 20
    out = tmp_path / "cropped.mp4"
    apply_crop(src, out, analysis)
    after = probe_media(out)
    assert after.width == 640
    assert after.height == 320


def test_real_two_pixel_border_is_preserved_as_real_crop(tmp_path: Path):
    src = tmp_path / "two-pixel.mp4"
    _make_segment(src, top=2, bottom=2, color="green", seconds=3.0)
    cfg = LibraryConfig.init(tmp_path / "library2")
    cfg.data["crop"]["scan_fps"] = 1.0
    analysis = analyze_crop(src, probe_media(src), cfg["crop"])
    assert analysis.verified_top == 2
    assert analysis.verified_bottom == 2
    assert analysis.safe_top == 2
    assert analysis.safe_bottom == 2
    assert analysis.method == "native_pixel_mask"


def test_brief_real_full_frame_state_blocks_vertical_crop(tmp_path: Path):
    bars = tmp_path / "bars.mp4"
    full = tmp_path / "full.mp4"
    _make_segment(bars, top=20, bottom=20, color="red", seconds=3.0)
    _make_segment(full, top=0, bottom=0, color="blue", seconds=1.0)
    listing = tmp_path / "list2.txt"
    listing.write_text(f"file '{bars}'\nfile '{full}'\nfile '{bars}'\n")
    src = tmp_path / "brief-full.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(src)],
        check=True,
    )
    cfg = LibraryConfig.init(tmp_path / "library3")
    cfg.data["crop"]["scan_fps"] = 1.0
    analysis = analyze_crop(src, probe_media(src), cfg["crop"])
    assert analysis.verified_top == 0
    assert analysis.verified_bottom == 0
    assert analysis.safe_top == 0
    assert analysis.safe_bottom == 0
