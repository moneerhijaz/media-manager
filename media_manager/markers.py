from __future__ import annotations

import json
from pathlib import Path

from .config import LibraryConfig
from .utils import now_iso


def marker_directory(media_path: Path, media_type: str, cfg: LibraryConfig) -> Path:
    if media_type == "movie":
        return media_path.parent
    # Prefer show-level marker: .../Show/Season 01/file -> .../Show
    for parent in media_path.parents:
        if parent.name.casefold().startswith("season "):
            return parent.parent
        if parent == cfg.tv_root:
            break
    return media_path.parent


def update_marker(media_path: Path, row, cfg: LibraryConfig) -> Path:
    folder = marker_directory(media_path, row["media_type"], cfg)
    marker = folder / ".media-manager.json"
    data = {"managed": True, "library_root": str(cfg.root), "items": {}, "updated_at": now_iso()}
    if marker.exists():
        try:
            old = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                data.update(old)
                data.setdefault("items", {})
        except Exception:
            pass
    rel = str(media_path.resolve().relative_to(cfg.root))
    data["managed"] = True
    data["library_root"] = str(cfg.root)
    data["updated_at"] = now_iso()
    # Drop stale path entries from markers that moved with a renamed folder/file.
    data["items"] = {
        k: v for k, v in (data.get("items") or {}).items()
        if (cfg.root / k).exists()
    }
    data["items"][rel] = {"media_id": int(row["id"]), "status": row["overall_status"]}
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(marker)
    return marker
