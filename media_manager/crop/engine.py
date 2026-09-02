from __future__ import annotations

import bisect
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..checkpoints import CheckpointStore, source_signature
from ..models import CropAnalysis, CropSample, ProbeInfo, StageStatus
from ..utils import MediaManagerError, require_tool, run


CROP_ALGORITHM_VERSION = "crop-v4-native-pixel-mask"


def _dark(sample: CropSample, yavg_limit: float, yhigh_limit: float) -> bool:
    if sample.yavg is None or sample.yhigh is None:
        return False
    return sample.yavg <= yavg_limit and sample.yhigh <= yhigh_limit


def _state_key(sample: CropSample) -> str:
    return f"{sample.left}:{sample.right}:{sample.top}:{sample.bottom}"


def _parse_state(key: str) -> tuple[int, int, int, int]:
    a, b, c, d = key.split(":")
    return int(a), int(b), int(c), int(d)


def _valid_samples(
    samples: list[CropSample],
    yavg: float,
    yhigh: float,
    *,
    fade_guard_seconds: float = 0.0,
    fade_yavg: float | None = None,
) -> tuple[list[CropSample], int]:
    """Exclude black frames and low-luma neighbors of black frames.

    A black/fade sample is *ignored*, not interpreted as either full-frame or
    giant bars. That prevents fade-to-black geometry from affecting the safe
    border decision.
    """
    usable = [s for s in samples if s.valid]
    dark_times = sorted(s.time for s in usable if _dark(s, yavg, yhigh))

    def near_black_transition(sample: CropSample) -> bool:
        if not dark_times or fade_guard_seconds <= 0 or fade_yavg is None:
            return False
        if sample.yavg is None or sample.yavg > fade_yavg:
            return False
        i = bisect.bisect_left(dark_times, sample.time)
        neighbors: list[float] = []
        if i < len(dark_times):
            neighbors.append(dark_times[i])
        if i:
            neighbors.append(dark_times[i - 1])
        return any(abs(sample.time - t) <= fade_guard_seconds for t in neighbors)

    valid: list[CropSample] = []
    ignored = 0
    for sample in usable:
        if _dark(sample, yavg, yhigh) or near_black_transition(sample):
            ignored += 1
            continue
        valid.append(sample)
    return valid, ignored


def _read_exact(stream, n: int) -> bytes:
    parts: list[bytes] = []
    remaining = n
    while remaining:
        block = stream.read(remaining)
        if not block:
            break
        parts.append(block)
        remaining -= len(block)
    return b"".join(parts)


def _leading_true(mask: np.ndarray) -> int:
    false = np.flatnonzero(~mask)
    return int(false[0]) if false.size else int(mask.size)


def _pixel_margins(frame: np.ndarray, *, black_luma: int, black_fraction: float, max_stddev: float) -> tuple[int, int, int, int]:
    """Measure permanently-black-looking edge rows/columns in native pixels.

    A border row/column is unused only when almost every pixel is at/below the
    configured black luma and the strip has low variance. This deliberately
    allows a genuine 1-2 px border while rejecting cropdetect-style jitter.
    """
    black = frame <= int(black_luma)
    row_fraction = black.mean(axis=1)
    col_fraction = black.mean(axis=0)
    # The standard deviation requirement catches dark-but-detailed picture
    # content that happens to have a low average luminance.
    row_std = frame.std(axis=1)
    col_std = frame.std(axis=0)
    rows_unused = (row_fraction >= float(black_fraction)) & (row_std <= float(max_stddev))
    cols_unused = (col_fraction >= float(black_fraction)) & (col_std <= float(max_stddev))
    top = _leading_true(rows_unused)
    bottom = _leading_true(rows_unused[::-1])
    left = _leading_true(cols_unused)
    right = _leading_true(cols_unused[::-1])
    return left, right, top, bottom


def _scan(
    path: Path,
    *,
    source_width: int,
    source_height: int,
    fps: float,
    limit: float | None = None,
    round_value: int | None = None,
    start: float | None = None,
    duration: float | None = None,
    pixel_black_luma: int = 28,
    pixel_black_fraction: float = 0.995,
    pixel_max_stddev: float = 12.0,
    frame_progress: Callable[[int], None] | None = None,
) -> tuple[list[CropSample], int, int]:
    """Stream source-resolution grayscale frames and inspect the real pixels.

    `limit`/`round_value` remain in the signature for compatibility with older
    callers/checkpoint tests; v0.4 no longer lets cropdetect decide geometry.
    """
    ffmpeg = require_tool("ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.6f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{max(0.05, duration):.6f}"]
    cmd += [
        "-map", "0:v:0", "-an", "-sn",
        "-vf", f"fps={float(fps):g},format=gray",
        "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    frame_size = int(source_width) * int(source_height)
    samples: list[CropSample] = []
    i = 0
    origin = float(start or 0.0)
    while True:
        raw = _read_exact(proc.stdout, frame_size)
        if not raw:
            break
        if len(raw) != frame_size:
            proc.kill()
            raise MediaManagerError(f"Short raw frame while scanning crop pixels ({len(raw)}/{frame_size} bytes)")
        frame = np.frombuffer(raw, dtype=np.uint8).reshape((source_height, source_width))
        yavg = float(frame.mean())
        yhigh = float(np.percentile(frame, 90.0))
        left, right, top, bottom = _pixel_margins(
            frame,
            black_luma=int(pixel_black_luma),
            black_fraction=float(pixel_black_fraction),
            max_stddev=float(pixel_max_stddev),
        )
        samples.append(CropSample(origin + i / max(float(fps), 0.001), left, right, top, bottom, yavg, yhigh, True))
        i += 1
        if frame_progress:
            frame_progress(i)
    stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", errors="replace")
    rc = proc.wait()
    if rc != 0:
        raise MediaManagerError(f"ffmpeg native-pixel crop scan failed: {stderr[-3000:]}")
    return samples, source_width, source_height


def _sample_from_dict(data: dict[str, Any]) -> CropSample:
    return CropSample(
        time=float(data["time"]), left=int(data["left"]), right=int(data["right"]),
        top=int(data["top"]), bottom=int(data["bottom"]),
        yavg=float(data["yavg"]) if data.get("yavg") is not None else None,
        yhigh=float(data["yhigh"]) if data.get("yhigh") is not None else None,
        valid=bool(data.get("valid", True)),
    )


def _analysis_from_dict(data: dict[str, Any]) -> CropAnalysis:
    fields = {
        "status": StageStatus(str(data["status"])),
        "variable": bool(data.get("variable", False)),
        "safe_left": int(data.get("safe_left", 0)), "safe_right": int(data.get("safe_right", 0)),
        "safe_top": int(data.get("safe_top", 0)), "safe_bottom": int(data.get("safe_bottom", 0)),
        "verified_left": int(data.get("verified_left", data.get("safe_left", 0))),
        "verified_right": int(data.get("verified_right", data.get("safe_right", 0))),
        "verified_top": int(data.get("verified_top", data.get("safe_top", 0))),
        "verified_bottom": int(data.get("verified_bottom", data.get("safe_bottom", 0))),
        "sample_count": int(data.get("sample_count", 0)), "ignored_dark": int(data.get("ignored_dark", 0)),
        "states": {str(k): int(v) for k, v in (data.get("states") or {}).items()},
        "raw_states": {str(k): int(v) for k, v in (data.get("raw_states") or {}).items()},
        "normalized_states": {str(k): int(v) for k, v in (data.get("normalized_states") or {}).items()},
        "meaningful_states": {str(k): int(v) for k, v in (data.get("meaningful_states") or {}).items()},
        "verification_times": [float(x) for x in (data.get("verification_times") or [])],
        "method": str(data.get("method") or "native_pixel_mask"),
        "algorithm_version": data.get("algorithm_version"),
        "crop_units": {str(k): int(v) for k, v in (data.get("crop_units") or {}).items()},
        "reason": data.get("reason"),
    }
    return CropAnalysis(**fields)


def _dedupe_samples(samples: list[CropSample]) -> list[CropSample]:
    by_time: dict[int, CropSample] = {}
    for sample in samples:
        by_time[int(round(sample.time * 1000.0))] = sample
    return [by_time[k] for k in sorted(by_time)]


def _state_distance(a: str, b: str) -> int:
    aa, bb = _parse_state(a), _parse_state(b)
    return max(abs(x - y) for x, y in zip(aa, bb))


def _normalize_states(samples: list[CropSample], cfg: dict, fps: float) -> tuple[Counter, Counter, Counter]:
    raw = Counter(_state_key(s) for s in samples)
    if not samples:
        return raw, Counter(), Counter()
    min_persist = float(cfg.get("state_min_persistence_seconds", 3.0))
    recur = max(2, int(cfg.get("state_min_recurrence_samples", 8)))
    span = max(0.0, samples[-1].time - samples[0].time)
    # Tiny test clips/trailers cannot exhibit a 3-second state if the whole
    # clip is only a few seconds long. Adapt only downward for short media;
    # feature-length content retains the configured persistence standard.
    min_persist = min(min_persist, max(1.0, span / 10.0))
    recur = min(recur, max(2, len(samples) // 10 or 2))
    tolerance = max(0, int(cfg.get("state_jitter_tolerance_pixels", 2)))

    max_run: dict[str, int] = defaultdict(int)
    cur_key: str | None = None
    cur_n = 0
    prev_t: float | None = None
    max_gap = 1.6 / max(fps, 0.001)
    for s in sorted(samples, key=lambda x: x.time):
        k = _state_key(s)
        continuous = cur_key == k and prev_t is not None and (s.time - prev_t) <= max_gap
        if continuous:
            cur_n += 1
        else:
            if cur_key is not None:
                max_run[cur_key] = max(max_run[cur_key], cur_n)
            cur_key, cur_n = k, 1
        prev_t = s.time
    if cur_key is not None:
        max_run[cur_key] = max(max_run[cur_key], cur_n)

    persistent = {
        k for k, n in raw.items()
        if (max_run.get(k, 0) / max(fps, 0.001)) >= min_persist or n >= recur
    }
    if not persistent:
        persistent = {raw.most_common(1)[0][0]}

    normalized = Counter()
    for k, n in raw.items():
        if k in persistent:
            normalized[k] += n
            continue
        nearest = sorted((( _state_distance(k, p), p) for p in persistent), key=lambda x: (x[0], -raw[x[1]]))
        if nearest and nearest[0][0] <= tolerance:
            normalized[nearest[0][1]] += n
        else:
            normalized[k] += n

    meaningful = Counter({k: normalized[k] for k in persistent if normalized[k]})
    return raw, normalized, meaningful


def _crop_units_from_probe(probe: ProbeInfo) -> tuple[int, int]:
    stream = next((s for s in probe.streams if s.get("codec_type") == "video"), {})
    pix = str(stream.get("pix_fmt") or "").casefold()
    # H.264 frame cropping is constrained by chroma subsampling. Conservative
    # defaults for the common 4:2:0 case are 2x2 source pixels.
    if "444" in pix or pix.startswith("gray"):
        return 1, 1
    if "422" in pix:
        return 2, 1
    return 2, 2


def _floor_unit(v: int, unit: int) -> int:
    return max(0, int(v) // max(1, int(unit)) * max(1, int(unit)))


def analyze_crop(
    path: Path,
    probe: ProbeInfo,
    cfg: dict,
    *,
    checkpoint_store: CheckpointStore | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> CropAnalysis:
    if not probe.width or not probe.height:
        return CropAnalysis(StageStatus.FAILED, False, reason="Video dimensions unavailable", algorithm_version=CROP_ALGORITHM_VERSION)
    if probe.video_codec != "h264":
        return CropAnalysis(StageStatus.NOT_APPLICABLE, False, reason=f"No lossless metadata crop backend implemented for codec {probe.video_codec!r}", algorithm_version=CROP_ALGORITHM_VERSION)

    sig = source_signature(path)
    cfg_key = json.dumps({
        "algorithm": CROP_ALGORITHM_VERSION,
        "scan_fps": cfg.get("scan_fps"), "transition_scan_fps": cfg.get("transition_scan_fps"),
        "black_yavg": cfg.get("black_yavg"), "black_yhigh": cfg.get("black_yhigh"),
        "fade_guard_seconds": cfg.get("fade_guard_seconds"), "fade_yavg": cfg.get("fade_yavg"),
        "pixel_black_luma": cfg.get("pixel_black_luma"), "pixel_black_fraction": cfg.get("pixel_black_fraction"),
        "pixel_max_stddev": cfg.get("pixel_max_stddev"), "state_min_persistence_seconds": cfg.get("state_min_persistence_seconds"),
        "state_min_recurrence_samples": cfg.get("state_min_recurrence_samples"), "state_jitter_tolerance_pixels": cfg.get("state_jitter_tolerance_pixels"),
    }, sort_keys=True)
    cp: dict[str, Any] = checkpoint_store.load("crop") if checkpoint_store else {}
    if cp.get("source_signature") != sig or cp.get("config_key") != cfg_key:
        cp = {"version": 4, "algorithm_version": CROP_ALGORITHM_VERSION, "source_signature": sig, "config_key": cfg_key, "baseline_chunks": {}, "refined": {}}
        if checkpoint_store:
            checkpoint_store.save("crop", cp)
    if isinstance(cp.get("final_analysis"), dict):
        return _analysis_from_dict(cp["final_analysis"])

    fps = float(cfg.get("scan_fps", 0.2))
    chunk_seconds = max(10.0, float(cfg.get("checkpoint_seconds", 60.0)))
    total_chunks = max(1, int(math.ceil(max(probe.duration, 0.001) / chunk_seconds)))
    chunks = cp.setdefault("baseline_chunks", {})
    for idx in range(total_chunks):
        key = str(idx)
        if key in chunks:
            continue
        start_t = idx * chunk_seconds
        duration = min(chunk_seconds, max(0.05, probe.duration - start_t))
        expected_frames = max(1, int(math.ceil(duration * fps)))
        def frame_tick(n: int, *, idx=idx, expected_frames=expected_frames) -> None:
            if progress:
                progress({
                    "phase": "baseline", "completed_chunks": idx, "total_chunks": total_chunks,
                    "chunk_frame": n, "chunk_frames": expected_frames, "checkpoint_seconds": chunk_seconds,
                    "sample_interval_seconds": 1.0 / max(fps, 0.001),
                    "valid_frames": sum(len(v) for v in chunks.values()),
                })
        chunk_samples, _, _ = _scan(
            path, source_width=probe.width, source_height=probe.height, fps=fps,
            start=start_t, duration=duration,
            pixel_black_luma=int(cfg.get("pixel_black_luma", 28)),
            pixel_black_fraction=float(cfg.get("pixel_black_fraction", 0.995)),
            pixel_max_stddev=float(cfg.get("pixel_max_stddev", 12.0)), frame_progress=frame_tick,
        )
        chunks[key] = [asdict(x) for x in chunk_samples]
        cp["phase"] = "baseline"; cp["baseline_completed"] = len(chunks); cp["baseline_total"] = total_chunks
        if checkpoint_store:
            checkpoint_store.save("crop", cp)
        if progress:
            progress({
                "phase": "baseline", "completed_chunks": len(chunks), "total_chunks": total_chunks,
                "checkpoint_seconds": chunk_seconds, "sample_interval_seconds": 1.0 / max(fps, 0.001),
                "valid_frames": sum(len(v) for v in chunks.values()),
            })

    baseline: list[CropSample] = []
    for idx in range(total_chunks):
        baseline.extend(_sample_from_dict(x) for x in chunks.get(str(idx), []))
    baseline = _dedupe_samples(baseline)
    baseline_valid, ignored = _valid_samples(
        baseline, float(cfg.get("black_yavg", 22)), float(cfg.get("black_yhigh", 38)),
        fade_guard_seconds=float(cfg.get("fade_guard_seconds", 2.0)), fade_yavg=float(cfg.get("fade_yavg", 55.0)),
    )
    if not baseline_valid:
        result = CropAnalysis(StageStatus.INSUFFICIENT_EVIDENCE, False, sample_count=0, ignored_dark=ignored, reason="No usable native-pixel crop samples", algorithm_version=CROP_ALGORITHM_VERSION)
        cp["final_analysis"] = {**asdict(result), "status": result.status.value}
        if checkpoint_store: checkpoint_store.save("crop", cp)
        return result

    raw_states, normalized_states, meaningful_states = _normalize_states(baseline_valid, cfg, fps)

    # Refine every baseline geometry change at higher FPS. Because the baseline
    # margins already come from native pixels, this catches brief full-frame
    # inserts without allowing cropdetect jitter to become the authority.
    intervals: list[tuple[float, float]] = []
    for a, b in zip(baseline_valid, baseline_valid[1:]):
        if a.margins != b.margins and 0.05 < b.time - a.time <= 2.5:
            intervals.append((a.time, b.time))
    intervals = intervals[:160]
    refined_cp = cp.setdefault("refined", {})
    for i, (a, b) in enumerate(intervals):
        key = f"{a:.3f}:{b:.3f}"
        if key not in refined_cp:
            start_t = max(0.0, a - 0.25)
            duration = min(1.75, max(0.5, b - a + 0.5))
            extra, _, _ = _scan(
                path, source_width=probe.width, source_height=probe.height,
                fps=float(cfg.get("transition_scan_fps", 5.0)), start=start_t, duration=duration,
                pixel_black_luma=int(cfg.get("pixel_black_luma", 28)),
                pixel_black_fraction=float(cfg.get("pixel_black_fraction", 0.995)),
                pixel_max_stddev=float(cfg.get("pixel_max_stddev", 12.0)),
            )
            v, _ = _valid_samples(extra, float(cfg.get("black_yavg", 22)), float(cfg.get("black_yhigh", 38)), fade_guard_seconds=float(cfg.get("fade_guard_seconds", 2.0)), fade_yavg=float(cfg.get("fade_yavg", 55.0)))
            refined_cp[key] = [asdict(x) for x in v]
            cp["phase"] = "transition_refine"
            if checkpoint_store: checkpoint_store.save("crop", cp)
        if progress:
            progress({"phase": "transition_refine", "completed_intervals": i + 1, "total_intervals": len(intervals), "valid_frames": len(baseline_valid)})

    all_valid = list(baseline_valid)
    verification_times: set[float] = set()
    for key, values in refined_cp.items():
        for x in values:
            sample = _sample_from_dict(x)
            all_valid.append(sample); verification_times.add(sample.time)
    all_valid = _dedupe_samples(all_valid)

    verified = {
        "left": min(s.left for s in all_valid), "right": min(s.right for s in all_valid),
        "top": min(s.top for s in all_valid), "bottom": min(s.bottom for s in all_valid),
    }
    unit_x, unit_y = _crop_units_from_probe(probe)
    applied = {
        "left": _floor_unit(verified["left"], unit_x), "right": _floor_unit(verified["right"], unit_x),
        "top": _floor_unit(verified["top"], unit_y), "bottom": _floor_unit(verified["bottom"], unit_y),
    }
    variable = len(meaningful_states) > 1
    has_verified = any(verified.values())
    has_applied = any(applied.values())
    if has_applied:
        status = StageStatus.COMPLETE
        reason = "Native-pixel evidence found a permanent unused border across every valid sampled frame"
    elif has_verified:
        status = StageStatus.NOT_NEEDED
        reason = "Permanent border exists but is smaller than the H.264/chroma crop unit; preserving pixels"
    else:
        status = StageStatus.NOT_NEEDED
        reason = "Native pixels show no permanent unused border across every valid sampled frame"

    result = CropAnalysis(
        status=status, variable=variable,
        safe_left=applied["left"], safe_right=applied["right"], safe_top=applied["top"], safe_bottom=applied["bottom"],
        verified_left=verified["left"], verified_right=verified["right"], verified_top=verified["top"], verified_bottom=verified["bottom"],
        sample_count=len(all_valid), ignored_dark=ignored,
        states=dict(normalized_states.most_common(30)), raw_states=dict(raw_states.most_common(30)),
        normalized_states=dict(normalized_states.most_common(30)), meaningful_states=dict(meaningful_states.most_common(30)),
        verification_times=sorted(verification_times), method="native_pixel_mask", algorithm_version=CROP_ALGORITHM_VERSION,
        crop_units={"horizontal": unit_x, "vertical": unit_y}, reason=reason,
    )
    cp["phase"] = "analysis_complete"; cp["final_analysis"] = {**asdict(result), "status": result.status.value}
    if checkpoint_store: checkpoint_store.save("crop", cp)
    return result


def _h264_existing_crop(path: Path) -> tuple[int, int, int, int]:
    """Return current SPS crop margins in pixels (left,right,top,bottom)."""
    ffmpeg = require_tool("ffmpeg")
    proc = run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(path),
        "-map", "0:v:0", "-c", "copy", "-bsf:v", "trace_headers", "-frames:v", "1",
        "-f", "null", "/dev/null",
    ], check=False)
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    def val(name: str, default: int = 0) -> int:
        m = re.search(rf"{re.escape(name)}\s+[^=]*=\s*(-?\d+)", text)
        return int(m.group(1)) if m else default
    chroma = val("chroma_format_idc", 1)
    separate = val("separate_colour_plane_flag", 0)
    frame_mbs_only = val("frame_mbs_only_flag", 1)
    if separate:
        chroma = 0
    if chroma == 0:
        sub_w, sub_h = 1, 1
    elif chroma == 1:
        sub_w, sub_h = 2, 2
    elif chroma == 2:
        sub_w, sub_h = 2, 1
    else:
        sub_w, sub_h = 1, 1
    unit_x = 1 if chroma == 0 else sub_w
    unit_y = (2 - frame_mbs_only) * (1 if chroma == 0 else sub_h)
    return (
        val("frame_crop_left_offset") * unit_x,
        val("frame_crop_right_offset") * unit_x,
        val("frame_crop_top_offset") * unit_y,
        val("frame_crop_bottom_offset") * unit_y,
    )

def apply_crop(path: Path, output: Path, analysis: CropAnalysis) -> None:
    if not analysis.has_crop:
        raise MediaManagerError("apply_crop called with zero crop")
    ffmpeg = require_tool("ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    existing_left, existing_right, existing_top, existing_bottom = _h264_existing_crop(path)
    # h264_metadata crop values are absolute relative to the coded frame. Preserve
    # any SPS crop already hiding macroblock padding, then add our newly detected
    # margins relative to the currently displayed frame.
    total_left = existing_left + analysis.safe_left
    total_right = existing_right + analysis.safe_right
    total_top = existing_top + analysis.safe_top
    total_bottom = existing_bottom + analysis.safe_bottom
    bsf = (
        f"h264_metadata=crop_left={total_left}:crop_right={total_right}:"
        f"crop_top={total_top}:crop_bottom={total_bottom}"
    )
    proc = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-map", "0", "-map_metadata", "0", "-map_chapters", "0",
        "-c", "copy", "-bsf:v:0", bsf, str(output),
    ])
    if not output.exists() or output.stat().st_size == 0:
        raise MediaManagerError(f"Crop output was not created: {output}")
