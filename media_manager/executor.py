from __future__ import annotations

import json
import os
import shutil
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .avsync import (
    AVSYNC_ALGORITHM_VERSION,
    analyze_av_sync,
    apply_verified_av_correction,
    classify_visual_type,
    evaluate_saved_observations,
)
from .checkpoints import CheckpointStore, source_signature
from .config import LibraryConfig
from .crop import CROP_ALGORITHM_VERSION, analyze_crop, apply_crop
from .database import Database
from .fingerprint import quick_fingerprint
from .markers import update_marker
from .models import StageStatus, VisualType
from .probe import probe_media
from .progress import ProgressDisplay
from .scanner import scan_library
from .subtitles import SUBTITLE_ALGORITHM_VERSION, OpenSubtitlesClient, QuotaExhausted, process_subtitle_language
from .utils import MediaManagerError, atomic_replace
from .verifier import ensure_work_space, verify_remux


STAGE_OK = {
    StageStatus.COMPLETE.value,
    StageStatus.NOT_NEEDED.value,
    StageStatus.NOT_APPLICABLE.value,
}
VISUAL_ALGORITHM_VERSION = "visual-v4-96frame"


STAGE_BLOCK = {
    StageStatus.NEEDS_REVIEW.value,
    StageStatus.FAILED.value,
    StageStatus.INSUFFICIENT_EVIDENCE.value,
    StageStatus.UNAVAILABLE.value,
}


class BatchStop(MediaManagerError):
    pass


def _details(row) -> dict[str, Any]:
    try:
        return json.loads(row["details_json"] or "{}")
    except Exception:
        return {}


def _stage_done(db: Database, media_id: int, stage: str) -> bool:
    row = db.get_stage(media_id, stage)
    return bool(row and row["status"] in STAGE_OK | STAGE_BLOCK)




def _expected_stage_version(name: str) -> str | None:
    if name == "visual_type":
        return VISUAL_ALGORITHM_VERSION
    if name == "av_sync":
        return AVSYNC_ALGORITHM_VERSION
    if name == "crop":
        return CROP_ALGORITHM_VERSION
    if name.startswith("subtitle:"):
        return SUBTITLE_ALGORITHM_VERSION
    return None


def _stage_needs_work(name: str, stage_row) -> bool:
    if stage_row is None or stage_row["status"] in {"pending", "running", "unknown"}:
        return True
    details = _details(stage_row)
    expected = _expected_stage_version(name)
    # v8 evidence remains opt-in for the v9 review policy. This avoids silently
    # reprocessing a completed library; `rerun-reviewed` performs the upgrade.
    if name == "av_sync" and details.get("algorithm_version") == "avsync-v8-syncnet-adapter-fix":
        return False
    if (
        name.startswith("subtitle:")
        and details.get("algorithm_version") == "subtitle-v5-full-runtime-primary"
    ):
        return False
    if expected is not None and details.get("algorithm_version") != expected:
        return True
    if name == "av_sync" and stage_row["status"] == StageStatus.INSUFFICIENT_EVIDENCE.value:
        return str(details.get("reason") or "").startswith("Visual type is unknown")
    return False


def _stage_selected(cfg: LibraryConfig, media_id: int, stage: str) -> bool:
    selected = cfg.data.get("_runtime", {}).get("selected_stages")
    if not isinstance(selected, dict):
        return True
    stages = selected.get(str(media_id), selected.get(media_id, []))
    if stage in stages:
        return True
    # A real A/V correction invalidates subtitle timing and final verification.
    # Those dependencies must be allowed to run if the selected A/V retry
    # changes the media, while unrelated successful stages remain untouched.
    return "av_sync" in stages and (stage.startswith("subtitle:") or stage == "final_verify")

def _refresh_after_write(cfg: LibraryConfig, db: Database, media_id: int, path: Path) -> None:
    p = probe_media(path)
    st = path.stat()
    fp = quick_fingerprint(path)
    db.update_after_manager_write(media_id, stat_result=st, fingerprint=fp, probe=p)


def _commit_media_temp(cfg: LibraryConfig, db: Database, media_id: int, source: Path, temp: Path) -> None:
    mode = source.stat().st_mode & 0o777
    atomic_replace(temp, source, mode)
    _refresh_after_write(cfg, db, media_id, source)


def _ui_progress(ui: ProgressDisplay | None, stage: str, details: dict[str, Any]) -> None:
    if ui is None:
        return
    phase = str(details.get("phase") or stage)
    if stage == "crop":
        if phase == "baseline":
            chunks = float(details.get("completed_chunks", 0))
            frame = float(details.get("chunk_frame", 0) or 0)
            frames = float(details.get("chunk_frames", 0) or 0)
            completed = chunks + (frame / frames if frames > 0 else 0.0)
            valid = details.get("valid_frames")
            interval = float(details.get("sample_interval_seconds", 0) or 0)
            msg = "native full-resolution pixel scan"
            if interval > 0:
                msg += f" every {interval:g}s"
            if valid is not None:
                msg += f"; sampled={valid}; checkpointed"
            ui.update(
                completed=completed,
                total=float(details.get("total_chunks", 0) or 0),
                message=msg,
            )
        elif phase == "transition_refine":
            ui.update(
                completed=float(details.get("completed_intervals", 0)),
                total=float(details.get("total_intervals", 0) or 0),
                message="refining aspect-ratio transitions",
            )
        elif phase == "native_verify":
            ui.update(
                completed=float(details.get("completed_checks", 0)),
                total=float(details.get("total_checks", 0) or 0),
                message="verifying crop boundaries at source resolution",
            )
        elif phase == "apply_crop":
            ui.update(completed=None, total=None, message="applying H.264 crop metadata and verifying remux")
    elif stage == "av_sync":
        if phase == "speech_scan":
            completed = details.get("completed")
            total = details.get("total")
            ui.update(
                completed=float(completed) if completed is not None else None,
                total=float(total) if total not in (None, 0, 0.0) else None,
                message=str(details.get("message") or "finding sustained speech"),
            )
        elif phase == "speech_scan_complete":
            ui.update(completed=None, total=None, message=f"speech scan complete: {details.get('segments', 0)} segments")
        elif phase == "candidates_selected":
            ui.update(completed=0, total=float(details.get("total", 0) or 0), message=str(details.get("message") or "selecting A/V candidates"))
        elif phase == "face_scoring":
            ui.update(
                completed=float(details.get("completed", 0)),
                total=float(details.get("total", 0) or 0),
                message="checking speech windows for visible face/mouth motion",
            )
        elif phase == "syncnet":
            ui.update(
                completed=float(details.get("completed", 0)),
                total=float(details.get("total", 0) or 0),
                message=str(details.get("message") or "running SyncNet on best distributed candidates"),
            )
        elif phase == "correction":
            ui.update(completed=None, total=None, message="testing and applying verified audio timestamp correction")


def _progress_callback(
    db: Database,
    media_id: int,
    stage: str,
    checkpoints: CheckpointStore,
    ui: ProgressDisplay | None = None,
):
    last_db_write = 0.0
    last_payload: dict[str, Any] = {}

    def update(details: dict[str, Any]) -> None:
        nonlocal last_db_write, last_payload
        payload = dict(details)
        payload.setdefault("checkpoint_path", str(checkpoints.path_for(stage)))
        now = time.monotonic()
        # The live renderer can update every frame, but committing SQLite on
        # every sampled movie-second would materially slow a fast crop scan.
        # Persist the visible progress snapshot at most once per real second;
        # actual reentrancy still comes from atomic checkpoint files.
        force = payload.get("phase") != last_payload.get("phase")
        if force or now - last_db_write >= 1.0:
            db.set_stage(media_id, stage, StageStatus.RUNNING, payload, __version__)
            db.set_overall(media_id, "incomplete")
            last_db_write = now
            last_payload = payload
        _ui_progress(ui, stage, details)
    return update


def _resume_or_commit_journal(
    cfg: LibraryConfig,
    db: Database,
    media_id: int,
    stage: str,
    source: Path,
    checkpoints: CheckpointStore,
) -> bool:
    """Finish a commit that may have been interrupted after analysis/verification.

    The journal is written before os.replace(). If a crash happens immediately
    after replacement, the output fingerprint lets a later run recognize that
    the transformation already landed and merely finish the database state.
    """
    cp = checkpoints.load(stage)
    if cp.get("phase") != "ready_to_commit" or not cp.get("output_fingerprint"):
        return False
    expected = str(cp["output_fingerprint"])
    details = cp.get("final_stage_details") or {}
    required_algorithm = _expected_stage_version(stage)
    if required_algorithm is not None and details.get("algorithm_version") != required_algorithm:
        # Never land a pre-upgrade transformation merely because its old temp
        # file survived a reboot. Keep ordinary analysis/work artifacts, but
        # discard the stale commit intent so the new stage version can decide.
        cp.pop("phase", None); cp.pop("output_fingerprint", None); cp.pop("commit_temp", None); cp.pop("final_stage_details", None)
        checkpoints.save(stage, cp)
        db.add_event(media_id, "stale_commit_discarded", f"Discarded outdated {stage} commit journal after algorithm upgrade")
        return False
    temp_value = cp.get("commit_temp")
    temp = Path(temp_value) if temp_value else None
    try:
        current = quick_fingerprint(source)
    except Exception:
        current = ""
    if current == expected:
        _refresh_after_write(cfg, db, media_id, source)
        db.set_stage(media_id, stage, StageStatus.COMPLETE, details, __version__)
        checkpoints.clear(stage)
        db.add_event(media_id, "resume_commit", f"Recovered completed {stage} commit after interruption")
        return True
    if temp is not None and temp.exists() and quick_fingerprint(temp) == expected:
        _commit_media_temp(cfg, db, media_id, source, temp)
        db.set_stage(media_id, stage, StageStatus.COMPLETE, details, __version__)
        checkpoints.clear(stage)
        db.add_event(media_id, "resume_commit", f"Completed pending {stage} commit from checkpoint")
        return True
    # The journal no longer corresponds to either the source or a usable temp.
    # Keep earlier analysis checkpoints only if the source signature still fits;
    # otherwise the analyzer will invalidate them itself.
    cp.pop("phase", None)
    cp.pop("output_fingerprint", None)
    cp.pop("commit_temp", None)
    cp.pop("final_stage_details", None)
    checkpoints.save(stage, cp)
    return False


def _journal_media_commit(
    cfg: LibraryConfig,
    db: Database,
    media_id: int,
    stage: str,
    source: Path,
    temp: Path,
    checkpoints: CheckpointStore,
    details: dict[str, Any],
) -> None:
    cp = checkpoints.load(stage)
    cp.update({
        "phase": "ready_to_commit",
        "output_fingerprint": quick_fingerprint(temp),
        "commit_temp": str(temp),
        "final_stage_details": details,
    })
    checkpoints.save(stage, cp)
    _commit_media_temp(cfg, db, media_id, source, temp)
    db.set_stage(media_id, stage, StageStatus.COMPLETE, details, __version__)
    checkpoints.clear(stage)


def _overall(db: Database, media_id: int, subtitle_languages: list[str]) -> str:
    stages = db.stages(media_id)
    relevant = ["identity", "integrity", "visual_type", "av_sync", "crop", "final_verify"]
    relevant += [f"subtitle:{x}" for x in subtitle_languages]
    if any(stage not in stages for stage in relevant):
        return "incomplete"
    statuses = [stages[s]["status"] for s in relevant]
    if any(s == StageStatus.FAILED.value for s in statuses):
        return "failed"
    if any(s in {StageStatus.NEEDS_REVIEW.value, StageStatus.INSUFFICIENT_EVIDENCE.value} for s in statuses):
        return "needs_review"
    if any(s == StageStatus.UNAVAILABLE.value for s in statuses):
        return "managed_with_gaps"
    if statuses and all(s in STAGE_OK for s in statuses):
        return "managed"
    return "incomplete"


def _visual_stage(path: Path, row, cfg: LibraryConfig, db: Database, ui: ProgressDisplay | None = None) -> VisualType:
    media_id = int(row["id"])
    if not _stage_selected(cfg, media_id, "visual_type"):
        return VisualType(db.get_media(media_id)["visual_type"])
    override = db.get_override(media_id, "visual_type")
    if override:
        visual = VisualType(override)
        db.set_visual_type(media_id, visual, 1.0)
        db.set_stage(media_id, "visual_type", StageStatus.COMPLETE, {"type": visual.value, "source": "override", "algorithm_version": VISUAL_ALGORITHM_VERSION}, __version__)
        return visual
    existing = db.get_stage(media_id, "visual_type")
    if existing and existing["status"] in STAGE_OK:
        previous = _details(existing)
        if previous.get("algorithm_version") == VISUAL_ALGORITHM_VERSION:
            return VisualType(db.get_media(media_id)["visual_type"])
    probe = probe_media(path)
    def visual_progress(details: dict[str, Any]) -> None:
        if ui is not None:
            ui.update(
                completed=float(details.get("completed", 0)),
                total=float(details.get("total", 0) or 0),
                message=f"sampling distributed frames ({details.get('usable', 0)} usable)",
            )
    visual, confidence, details = classify_visual_type(path, probe, cfg["av_sync"], progress=visual_progress)
    db.set_visual_type(media_id, visual, confidence)
    db.set_stage(media_id, "visual_type", StageStatus.COMPLETE, {"type": visual.value, "confidence": confidence, "algorithm_version": VISUAL_ALGORITHM_VERSION, **details}, __version__)
    return visual


def _av_stage(path: Path, row, cfg: LibraryConfig, db: Database, work: Path, visual: VisualType, ui: ProgressDisplay | None = None) -> None:
    media_id = int(row["id"])
    if not _stage_selected(cfg, media_id, "av_sync"):
        return
    existing_av = db.get_stage(media_id, "av_sync")
    existing_details = _details(existing_av) if existing_av else {}
    saved_review = existing_details.get("previous_review") if existing_details.get("retry_scope") == "reviewed_only" else None
    retry_legacy_unknown = False
    if existing_av and existing_av["status"] == StageStatus.INSUFFICIENT_EVIDENCE.value:
        previous = _details(existing_av)
        retry_legacy_unknown = str(previous.get("reason") or "").startswith("Visual type is unknown")
    if not _stage_needs_work("av_sync", existing_av) and not retry_legacy_unknown:
        return
    if cfg.data.get("_runtime", {}).get("skip_av_sync"):
        return
    if not cfg["av_sync"].get("enabled", True):
        db.set_stage(media_id, "av_sync", StageStatus.NOT_APPLICABLE, {"reason": "disabled in library config", "algorithm_version": AVSYNC_ALGORITHM_VERSION}, __version__)
        return
    checkpoints = CheckpointStore(cfg.state_dir, media_id)
    if _resume_or_commit_journal(cfg, db, media_id, "av_sync", path, checkpoints):
        return
    progress = _progress_callback(db, media_id, "av_sync", checkpoints, ui)
    db.set_stage(media_id, "av_sync", StageStatus.RUNNING, {
        "phase": "analysis",
        "checkpoint_path": str(checkpoints.path_for("av_sync")),
    }, __version__)
    try:
        probe = probe_media(path)
        analysis = None
        if isinstance(saved_review, dict):
            analysis = evaluate_saved_observations(
                saved_review, cfg["av_sync"], duration=probe.duration, visual_type=visual
            )
            if analysis is not None:
                analysis.diagnostics["review_retry"] = "reused_saved_syncnet_observations"
                progress({"phase": "analysis_complete", "message": "Re-evaluated saved SyncNet evidence"})
        if analysis is None:
            analysis = analyze_av_sync(
                path, probe, cfg.root, cfg["av_sync"], visual,
                checkpoint_store=checkpoints, work_dir=work, progress=progress,
            )
        details = asdict(analysis)
        details["status"] = analysis.status.value
        details["algorithm_version"] = AVSYNC_ALGORITHM_VERSION
        if analysis.status == StageStatus.INSUFFICIENT_EVIDENCE:
            # An inconclusive lip-sync check is a verification gap, not proof
            # that the media needs repair. Preserve all evidence for inspection.
            details["analysis_status"] = analysis.status.value
            details["status"] = StageStatus.UNAVAILABLE.value
            db.set_stage(media_id, "av_sync", StageStatus.UNAVAILABLE, details, __version__)
            checkpoints.clear("av_sync")
            return
        if analysis.status != StageStatus.COMPLETE:
            db.set_stage(media_id, "av_sync", analysis.status, details, __version__)
            checkpoints.clear("av_sync")
            return
        if not cfg["av_sync"].get("apply_corrections", True):
            db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, {**details, "reason": "Correction recommended but automatic correction disabled"}, __version__)
            checkpoints.clear("av_sync")
            return
        ensure_work_space(cfg.state_dir, path)
        temp = work / "av-corrected.mp4"
        progress({"phase": "correction"})
        correction = apply_verified_av_correction(path, temp, analysis, probe, cfg.root, cfg["av_sync"])
        after = probe_media(temp)
        verify = verify_remux(probe, after)
        final_details = {**details, "correction": correction, "verification": verify}
        _journal_media_commit(cfg, db, media_id, "av_sync", path, temp, checkpoints, final_details)
        # A/V correction is explicitly video stream-copy. Crop geometry checkpoints
        # therefore remain valid even though the MP4 container size/mtime changed.
        # Rebind an existing crop checkpoint to the verified remux so a resumed
        # full-resolution crop scan does not discard already-scanned chunks.
        crop_cp = checkpoints.load("crop")
        if crop_cp.get("baseline_chunks"):
            crop_cp["source_signature"] = source_signature(path)
            checkpoints.save("crop", crop_cp)
        # A real A/V timeline correction invalidates subtitle synchronization
        # and final verification, but not crop geometry.
        db.conn.execute("DELETE FROM stages WHERE media_id=? AND (stage LIKE 'subtitle:%' OR stage='final_verify')", (media_id,))
        db.conn.commit()
        db.add_event(media_id, "av_sync", "Applied verified A/V timestamp correction", correction)
    except KeyboardInterrupt:
        cp = checkpoints.load("av_sync")
        db.set_stage(media_id, "av_sync", StageStatus.PENDING, {
            "reason": "paused/interrupted",
            "phase": cp.get("phase", "analysis"),
            "checkpoint_path": str(checkpoints.path_for("av_sync")),
        }, __version__)
        db.set_overall(media_id, "incomplete")
        raise
    except Exception as exc:
        cp = checkpoints.load("av_sync")
        # Preserve completed analysis and work files so a deliberate retry can
        # resume without re-running expensive SyncNet observations.  This is a
        # terminal review result, not a pending stage: recording the algorithm
        # version is essential or queue selection will retry it on every run.
        analysis_details = cp.get("final_analysis") if isinstance(cp.get("final_analysis"), dict) else {}
        error = str(exc)
        reason = _correction_review_reason(error)
        review_details = {
            **analysis_details,
            "status": StageStatus.NEEDS_REVIEW.value,
            "reason": reason,
            "correction_error": error,
            "error_type": type(exc).__name__,
            "phase": cp.get("phase"),
            "checkpoint_path": str(checkpoints.path_for("av_sync")),
            "analysis_checkpoint_preserved": bool(analysis_details),
            "algorithm_version": AVSYNC_ALGORITHM_VERSION,
        }
        db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, review_details, __version__)
        if ui is not None:
            ui.update(completed=None, total=None, message=f"correction withheld: {error}")


def _crop_stage(path: Path, row, cfg: LibraryConfig, db: Database, work: Path, ui: ProgressDisplay | None = None) -> None:
    media_id = int(row["id"])
    if not _stage_selected(cfg, media_id, "crop"):
        return
    existing_crop = db.get_stage(media_id, "crop")
    if not _stage_needs_work("crop", existing_crop):
        return
    if cfg.data.get("_runtime", {}).get("skip_crop"):
        return
    if not cfg["crop"].get("enabled", True):
        db.set_stage(media_id, "crop", StageStatus.NOT_APPLICABLE, {"reason": "disabled in library config", "algorithm_version": CROP_ALGORITHM_VERSION}, __version__)
        return
    checkpoints = CheckpointStore(cfg.state_dir, media_id)
    if _resume_or_commit_journal(cfg, db, media_id, "crop", path, checkpoints):
        return
    progress = _progress_callback(db, media_id, "crop", checkpoints, ui)
    db.set_stage(media_id, "crop", StageStatus.RUNNING, {
        "phase": "baseline",
        "checkpoint_path": str(checkpoints.path_for("crop")),
    }, __version__)
    try:
        probe = probe_media(path)
        crop_cfg = dict(cfg["crop"])
        crop_cfg["checkpoint_seconds"] = cfg["processing"].get("checkpoint_seconds", 60.0)
        analysis = analyze_crop(
            path, probe, crop_cfg,
            checkpoint_store=checkpoints, progress=progress,
        )
        details = asdict(analysis)
        details["status"] = analysis.status.value
        details["algorithm_version"] = CROP_ALGORITHM_VERSION
        details["source_width"] = probe.width
        details["source_height"] = probe.height
        if analysis.status != StageStatus.COMPLETE or not analysis.has_crop:
            db.set_stage(media_id, "crop", analysis.status, details, __version__)
            checkpoints.clear("crop")
            return
        ensure_work_space(cfg.state_dir, path)
        temp = work / "cropped.mp4"
        progress({"phase": "apply_crop"})
        apply_crop(path, temp, analysis)
        after = probe_media(temp)
        verify = verify_remux(
            probe, after,
            crop=(analysis.safe_left, analysis.safe_right, analysis.safe_top, analysis.safe_bottom),
        )
        final_details = {**details, "verification": verify}
        _journal_media_commit(cfg, db, media_id, "crop", path, temp, checkpoints, final_details)
        db.conn.execute("DELETE FROM stages WHERE media_id=? AND stage='final_verify'", (media_id,))
        db.conn.commit()
        db.add_event(media_id, "crop", "Applied safe common H.264 crop metadata", {
            "left": analysis.safe_left, "right": analysis.safe_right,
            "top": analysis.safe_top, "bottom": analysis.safe_bottom,
            "variable": analysis.variable,
        })
    except KeyboardInterrupt:
        cp = checkpoints.load("crop")
        db.set_stage(media_id, "crop", StageStatus.PENDING, {
            "reason": "paused/interrupted",
            "phase": cp.get("phase", "baseline"),
            "baseline_completed": cp.get("baseline_completed"),
            "baseline_total": cp.get("baseline_total"),
            "checkpoint_path": str(checkpoints.path_for("crop")),
        }, __version__)
        db.set_overall(media_id, "incomplete")
        raise
    except Exception as exc:
        cp = checkpoints.load("crop")
        db.set_stage(media_id, "crop", StageStatus.FAILED, {
            "error": str(exc),
            "phase": cp.get("phase"),
            "checkpoint_path": str(checkpoints.path_for("crop")),
        }, __version__)


def _subtitle_stages(path: Path, row, cfg: LibraryConfig, db: Database, work: Path, client: OpenSubtitlesClient | None, ui: ProgressDisplay | None = None, *, stage_start_index: int = 4, stage_total: int = 5) -> None:
    media_id = int(row["id"])
    if cfg.data.get("_runtime", {}).get("skip_subtitles"):
        return
    if not cfg["subtitles"].get("enabled", True):
        for language in cfg["subtitles"].get("languages", ["en"]):
            stage = f"subtitle:{language}"
            if not _stage_done(db, media_id, stage):
                db.set_stage(media_id, stage, StageStatus.NOT_APPLICABLE, {"reason": "disabled", "algorithm_version": SUBTITLE_ALGORITHM_VERSION}, __version__)
        return
    checkpoints = CheckpointStore(cfg.state_dir, media_id)
    for language in cfg["subtitles"].get("languages", ["en"]):
        stage = f"subtitle:{language}"
        if not _stage_selected(cfg, media_id, stage):
            continue
        if not _stage_needs_work(stage, db.get_stage(media_id, stage)):
            continue
        cp = checkpoints.load(stage)
        final = path.with_name(f"{path.stem}.{language}.srt")
        # Recover a subtitle commit interrupted after the sidecar was moved.
        if cp.get("phase") == "ready_to_commit":
            details = cp.get("final_stage_details") or {}
            if details.get("algorithm_version") != SUBTITLE_ALGORITHM_VERSION:
                cp.pop("phase", None); cp.pop("commit_temp", None); cp.pop("final_stage_details", None)
                checkpoints.save(stage, cp)
            else:
                temp_value = cp.get("commit_temp")
                temp = Path(temp_value) if temp_value else None
                if final.exists() and final.stat().st_size > 0:
                    db.set_stage(media_id, stage, StageStatus.COMPLETE, details, __version__)
                    checkpoints.clear(stage)
                    continue
                if temp is not None and temp.exists() and temp.stat().st_size > 0:
                    temp.replace(final)
                    db.set_stage(media_id, stage, StageStatus.COMPLETE, details, __version__)
                    checkpoints.clear(stage)
                    continue
        if ui is not None:
            ui.stage(f"subtitle:{language}", stage_index=stage_start_index, stage_total=stage_total, message=f"acquiring/validating {language} subtitles")
        stage_start_index += 1
        db.set_stage(media_id, stage, StageStatus.RUNNING, {
            "phase": "acquire_or_sync",
            "checkpoint_path": str(checkpoints.path_for(stage)),
            "cached_download": str(work / f"downloaded.{language}.srt"),
        }, __version__)
        current_row = db.get_media(media_id)
        try:
            status, details, temp_out = process_subtitle_language(
                path, current_row, str(language), cfg["subtitles"], client, work,
            )
            if status == StageStatus.COMPLETE and temp_out is not None:
                cp = checkpoints.load(stage)
                cp.update({
                    "phase": "ready_to_commit",
                    "commit_temp": str(temp_out),
                    "final_path": str(final),
                    "final_stage_details": details,
                })
                checkpoints.save(stage, cp)
                if final.exists():
                    final.unlink()
                temp_out.replace(final)
                db.add_event(media_id, "subtitle", f"Synchronized {language} subtitle", {"path": str(final)})
            db.set_stage(media_id, stage, status, details, __version__)
            if status != StageStatus.PENDING:
                checkpoints.clear(stage)
        except KeyboardInterrupt:
            cached = work / f"downloaded.{language}.srt"
            checkpoints.save(stage, {
                **checkpoints.load(stage),
                "phase": "paused",
                "cached_download": str(cached) if cached.exists() else None,
            })
            db.set_stage(media_id, stage, StageStatus.PENDING, {
                "reason": "paused/interrupted",
                "cached_download": str(cached) if cached.exists() else None,
                "checkpoint_path": str(checkpoints.path_for(stage)),
            }, __version__)
            db.set_overall(media_id, "incomplete")
            raise
        except QuotaExhausted as exc:
            db.set_stage(media_id, stage, StageStatus.PENDING, {"reason": str(exc)}, __version__)
            raise BatchStop(str(exc)) from exc
        except Exception as exc:
            db.set_stage(media_id, stage, StageStatus.FAILED, {
                "error": str(exc),
                "checkpoint_path": str(checkpoints.path_for(stage)),
                "cached_download": str(work / f"downloaded.{language}.srt") if (work / f"downloaded.{language}.srt").exists() else None,
            }, __version__)


def _completion_message(db: Database, media_id: int, status: str) -> str:
    stages = db.stages(media_id)
    if status in {"needs_review", "failed", "managed_with_gaps"}:
        for name, stage in stages.items():
            if stage["status"] in {"needs_review", "failed", "insufficient_evidence", "unavailable"}:
                d = _details(stage)
                reason = d.get("reason") or d.get("error") or d.get("correction_error") or stage["status"]
                correction_error = d.get("correction_error") or d.get("error")
                if correction_error and correction_error != reason:
                    return f"{name}: {reason} — {correction_error}"
                return f"{name}: {reason}"
    bits: list[str] = []
    crop = stages.get("crop")
    if crop:
        d = _details(crop)
        if crop["status"] == "complete":
            bits.append(f"crop={d.get('safe_left',0)}:{d.get('safe_right',0)}:{d.get('safe_top',0)}:{d.get('safe_bottom',0)}")
        elif crop["status"] == "not_needed":
            bits.append("crop=not-needed")
    for name, stage in stages.items():
        if name.startswith("subtitle:") and stage["status"] == "complete":
            d = _details(stage); f = d.get("ffsubsync") or {}
            if f.get("offset_seconds") is not None:
                bits.append(f"{name} offset={float(f['offset_seconds']):+.2f}s scale={f.get('scale')}")
    return "; ".join(bits[:2]) or status


def process_one(cfg: LibraryConfig, db: Database, row, client: OpenSubtitlesClient | None, verbose: bool = False, ui: ProgressDisplay | None = None) -> str:
    media_id = int(row["id"])
    path = cfg.root / row["rel_path"]
    if not path.exists():
        db.set_overall(media_id, "failed", "File disappeared")
        return "failed"
    if row["last_error"] and row["overall_status"] == "needs_review" and db.get_stage(media_id, "identity") and db.get_stage(media_id, "identity")["status"] == StageStatus.NEEDS_REVIEW.value:
        return "needs_review"

    work = cfg.state_dir / "work" / str(media_id)
    checkpoints = CheckpointStore(cfg.state_dir, media_id)
    # An externally replaced file must never inherit cached subtitle downloads,
    # crop chunks, or A/V observations from the previous contents.
    if row["overall_status"] == "changed":
        shutil.rmtree(work, ignore_errors=True)
        checkpoints.clear_all()
    work.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"\nPROCESS  {row['rel_path']}")
    final_status = "incomplete"
    languages = [str(x) for x in cfg["subtitles"].get("languages", ["en"])]
    stage_total = 4 + len(languages)
    try:
        if ui is not None:
            ui.stage("visual", stage_index=1, stage_total=stage_total, message="classifying live-action / animation")
        visual = _visual_stage(path, row, cfg, db, ui)
        if ui is not None:
            ui.stage("a/v sync", stage_index=2, stage_total=stage_total, message=f"authoritative audio; visual={visual.value}")
        _av_stage(path, db.get_media(media_id), cfg, db, work, visual, ui)
        if ui is not None:
            ui.stage("crop", stage_index=3, stage_total=stage_total, message="scanning full-resolution pixels")
        _crop_stage(path, db.get_media(media_id), cfg, db, work, ui)
        _subtitle_stages(path, db.get_media(media_id), cfg, db, work, client, ui, stage_start_index=4, stage_total=stage_total)
        if ui is not None:
            ui.stage("verify", stage_index=stage_total, stage_total=stage_total, message="final ffprobe/integrity verification")
        if _stage_selected(cfg, media_id, "final_verify"):
            final_probe = probe_media(path)
            db.set_stage(media_id, "final_verify", StageStatus.COMPLETE, {
                "duration": final_probe.duration, "video_codec": final_probe.video_codec,
                "width": final_probe.width, "height": final_probe.height,
                "audio_streams": len(final_probe.audio_streams),
            }, __version__)
        status = _overall(db, media_id, [str(x) for x in cfg["subtitles"].get("languages", ["en"])])
        final_status = status
        db.set_overall(media_id, status)
        current = db.get_media(media_id)
        if cfg["processing"].get("create_markers", True) and status in {"managed", "managed_with_gaps", "needs_review"}:
            marker = update_marker(path, current, cfg)
            db.set_stage(media_id, "marker", StageStatus.COMPLETE, {"path": str(marker)}, __version__)
        return status
    except KeyboardInterrupt:
        db.set_overall(media_id, "incomplete")
        db.add_event(media_id, "pause", "Processing paused; checkpoint state preserved")
        raise
    except BatchStop:
        raise
    except Exception as exc:
        final_status = "failed"
        db.set_overall(media_id, "failed", str(exc))
        db.add_event(media_id, "failure", str(exc))
        return "failed"
    finally:
        if final_status in {"managed", "managed_with_gaps"}:
            shutil.rmtree(work, ignore_errors=True)
            checkpoints.clear_all()
        elif not cfg["processing"].get("preserve_incomplete_work", True) and final_status != "incomplete":
            shutil.rmtree(work, ignore_errors=True)


def _recover_pending_media_commits(cfg: LibraryConfig, db: Database) -> int:
    recovered = 0
    for row in db.rows("present=1"):
        media_id = int(row["id"])
        path = cfg.root / row["rel_path"]
        if not path.exists():
            continue
        checkpoints = CheckpointStore(cfg.state_dir, media_id)
        for stage in ("av_sync", "crop"):
            if _resume_or_commit_journal(cfg, db, media_id, stage, path, checkpoints):
                recovered += 1
    return recovered


def _correction_review_reason(error: str) -> str:
    if "did not validate" in error:
        return "Automatic A/V correction withheld because independent sign validation did not sufficiently improve alignment"
    if "Could not validate either A/V correction direction" in error:
        return "Automatic A/V correction withheld because neither shift direction could be independently validated"
    if "Multiple audio streams present" in error:
        return "Automatic A/V correction withheld because the file has multiple audio streams and the configured policy requires review"
    return "Automatic A/V correction withheld after completed checkpointed analysis"


def _upgrade_legacy_av_correction_reviews(cfg: LibraryConfig, db: Database) -> int:
    """Repair the v0.4.0 review record that omitted algorithm_version.

    Without this migration, queue selection treats the terminal review as an
    outdated stage and re-runs the same completed A/V checkpoint on every
    invocation.  Only migrate records whose preserved checkpoint explicitly
    belongs to the current A/V algorithm.
    """
    upgraded = 0
    rows = db.conn.execute(
        "SELECT media_id,details_json FROM stages WHERE stage='av_sync' AND status=?",
        (StageStatus.NEEDS_REVIEW.value,),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except Exception:
            continue
        if details.get("algorithm_version"):
            continue
        if details.get("reason") != "A/V processing failed after checkpointed analysis":
            continue
        media_id = int(row["media_id"])
        cp = CheckpointStore(cfg.state_dir, media_id).load("av_sync")
        final = cp.get("final_analysis") if isinstance(cp.get("final_analysis"), dict) else {}
        config_key = str(cp.get("config_key") or "")
        if not final or AVSYNC_ALGORITHM_VERSION not in config_key:
            continue
        error = str(details.get("error") or details.get("correction_error") or "Unknown A/V correction error")
        repaired = {
            **final,
            "status": StageStatus.NEEDS_REVIEW.value,
            "reason": _correction_review_reason(error),
            "correction_error": error,
            "error_type": details.get("error_type"),
            "phase": cp.get("phase"),
            "checkpoint_path": str(CheckpointStore(cfg.state_dir, media_id).path_for("av_sync")),
            "analysis_checkpoint_preserved": True,
            "algorithm_version": AVSYNC_ALGORITHM_VERSION,
        }
        db.set_stage(media_id, "av_sync", StageStatus.NEEDS_REVIEW, repaired, __version__)
        db.add_event(media_id, "state_migration", "Repaired v0.4.0 A/V correction review record without re-running SyncNet")
        upgraded += 1
    return upgraded


def recover_reentrant_state(cfg: LibraryConfig, db: Database) -> dict[str, int]:
    recovered = db.recover_interrupted_state()
    commits = _recover_pending_media_commits(cfg, db)
    av_reviews = _upgrade_legacy_av_correction_reviews(cfg, db)
    return {**recovered, "commits": commits, "av_review_migrations": av_reviews}


def scan_and_run(
    cfg: LibraryConfig,
    db: Database,
    *,
    limit: int | None = None,
    verbose: bool = False,
    client: OpenSubtitlesClient | None = None,
    progress_enabled: bool = True,
    scan: bool = True,
    media_ids: list[int] | None = None,
) -> dict[str, int]:
    ui = ProgressDisplay(enabled=None if progress_enabled else False)
    if client is not None:
        # OpenSubtitles asks for the account password only when a download is
        # actually needed. Route that lazy prompt through the live display so
        # its renderer cannot erase the question and leave the run apparently
        # hung while getpass waits for input.
        client.password_prompt = ui.prompt_secret
    if scan:
        ui.scan("Scanning Movies/ and TV Shows/ recursively")
        try:
            scan_library(cfg, db, verbose=False)
        except Exception:
            ui.close()
            raise
    else:
        ui.scan("Using reviewed items already recorded in the database (library scan skipped)")
    if media_ids is None:
        rows = db.rows("present=1")
    elif not media_ids:
        rows = []
    else:
        placeholders = ",".join("?" for _ in media_ids)
        rows = db.rows(f"present=1 AND id IN ({placeholders})", media_ids)
    # managed_with_gaps/review/failed have terminal stage records and therefore run cheaply;
    # keep them in the queue only if they still contain genuinely pending stages.
    pending = []
    for row in rows:
        stages = db.stages(int(row["id"]))
        if "identity" in stages and stages["identity"]["status"] == StageStatus.NEEDS_REVIEW.value:
            continue
        enabled_names = ["visual_type"]
        runtime = cfg.data.get("_runtime", {})
        if cfg["av_sync"].get("enabled", True) and not runtime.get("skip_av_sync"):
            enabled_names.append("av_sync")
        if cfg["crop"].get("enabled", True) and not runtime.get("skip_crop"):
            enabled_names.append("crop")
        if cfg["subtitles"].get("enabled", True) and not runtime.get("skip_subtitles"):
            enabled_names += [f"subtitle:{x}" for x in cfg["subtitles"].get("languages", ["en"])]
        enabled_names.append("final_verify")
        enabled_names = [name for name in enabled_names if _stage_selected(cfg, int(row["id"]), name)]
        if any(_stage_needs_work(name, stages.get(name)) for name in enabled_names):
            pending.append(row)
    if limit is not None:
        pending = pending[:limit]
    counts: dict[str, int] = {}
    try:
        for i, row in enumerate(pending, 1):
            stage_total = 4 + len(cfg["subtitles"].get("languages", ["en"]))
            ui.start_file(i, len(pending), row["rel_path"], stage_total=stage_total)
            try:
                status = process_one(cfg, db, row, client, verbose=verbose, ui=ui)
            except BatchStop as exc:
                ui.finish_file("stopped", str(exc))
                counts["quota_stop"] = counts.get("quota_stop", 0) + 1
                break
            except KeyboardInterrupt:
                ui.finish_file("paused", "checkpoint preserved")
                raise
            ui.finish_file(status, _completion_message(db, int(row["id"]), status))
            counts[status] = counts.get(status, 0) + 1
        return counts
    finally:
        ui.close()
