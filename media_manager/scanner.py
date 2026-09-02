from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import LibraryConfig
from .database import Database
from .fingerprint import quick_fingerprint
from .identify import identify
from .probe import probe_media
from .utils import MediaManagerError


@dataclass(slots=True)
class ScanStats:
    discovered: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    ignored: int = 0
    conflicts: int = 0
    failures: int = 0
    missing: int = 0
    moved: int = 0


def iter_media(cfg: LibraryConfig):
    extensions = {str(x).casefold() for x in cfg["library"]["extensions"]}
    for base in (cfg.movies_root, cfg.tv_root):
        if not base.exists():
            continue
        for p in sorted(base.rglob("*"), key=lambda x: str(x).casefold()):
            if p.is_file() and p.suffix.casefold() in extensions:
                yield p


def scan_library(cfg: LibraryConfig, db: Database, verbose: bool = False) -> ScanStats:
    stats = ScanStats()
    db.begin_scan()
    for path in iter_media(cfg):
        stats.discovered += 1
        identity = identify(path, cfg)
        if identity is None:
            stats.ignored += 1
            continue
        rel = str(path.resolve().relative_to(cfg.root))
        st = path.stat()
        existing = db.get_by_rel_path(rel)
        if existing is not None:
            db.mark_present_rel(rel)
        try:
            # Cheap skip: if size/mtime are identical, the stored fingerprint and probe remain valid.
            if existing is not None and existing["size"] == st.st_size and existing["mtime_ns"] == st.st_mtime_ns:
                db.mark_seen_only(int(existing["id"]), st)
                stats.unchanged += 1
                if identity.conflict:
                    stats.conflicts += 1
                    db.set_overall(int(existing["id"]), "needs_review", identity.conflict)
                continue
            fp = quick_fingerprint(path)
            probe = probe_media(path)
            manager_expected = bool(existing and existing["manager_output_fingerprint"] == fp)
            media_id, changed, created = db.upsert_media(
                rel_path=rel,
                identity=identity,
                stat_result=st,
                fingerprint=fp,
                probe=probe,
                external_change=not manager_expected,
            )
            if created:
                stats.new += 1
            elif existing is None:
                stats.moved += 1
            elif changed:
                stats.changed += 1
            else:
                stats.unchanged += 1
            if identity.conflict:
                stats.conflicts += 1
                db.set_overall(media_id, "needs_review", identity.conflict)
                db.set_stage(media_id, "identity", "needs_review", {"reason": identity.conflict})
            else:
                db.set_stage(media_id, "identity", "complete", {
                    "media_type": identity.media_type.value,
                    "title": identity.title,
                    "year": identity.year,
                    "show_title": identity.show_title,
                    "season": identity.season,
                    "episode": identity.episode,
                })
            db.set_stage(media_id, "integrity", "complete", {
                "duration": probe.duration,
                "video_codec": probe.video_codec,
                "width": probe.width,
                "height": probe.height,
                "audio_streams": len(probe.audio_streams),
                "subtitle_streams": len(probe.subtitle_streams),
            })
            if verbose:
                print(f"SCAN  {rel}")
        except Exception as exc:
            stats.failures += 1
            if existing:
                db.set_overall(int(existing["id"]), "failed", str(exc))
            if verbose:
                print(f"FAIL  {rel}: {exc}")
    stats.missing = len(db.rows("present=0"))
    return stats
