from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .utils import now_iso


class CheckpointStore:
    """Atomic per-media, per-stage checkpoint storage.

    Checkpoints are intentionally plain JSON so they are inspectable and survive
    interpreter crashes, Ctrl+C, SSH disconnects, and reboots.
    """

    def __init__(self, state_dir: Path, media_id: int):
        self.root = state_dir / "checkpoints" / str(media_id)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, stage: str) -> Path:
        safe = stage.replace(":", "_").replace("/", "_")
        return self.root / f"{safe}.json"

    def load(self, stage: str) -> dict[str, Any]:
        path = self.path_for(stage)
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            # A torn/corrupt checkpoint must never block the media pipeline.
            return {}

    def save(self, stage: str, data: dict[str, Any]) -> Path:
        path = self.path_for(stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["checkpoint_updated_at"] = now_iso()
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # fsync the directory as well as the JSON file.  The rename is atomic,
        # but without this step its directory entry is not guaranteed durable
        # across a sudden power loss.
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return path

    def clear(self, stage: str) -> None:
        self.path_for(stage).unlink(missing_ok=True)

    def clear_all(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.glob("*.json"):
            path.unlink(missing_ok=True)
        try:
            self.root.rmdir()
        except OSError:
            pass


def source_signature(path: Path) -> dict[str, int]:
    st = path.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def signature_matches(checkpoint: dict[str, Any], path: Path) -> bool:
    sig = checkpoint.get("source_signature")
    if not isinstance(sig, dict):
        return False
    cur = source_signature(path)
    try:
        return int(sig.get("size")) == cur["size"] and int(sig.get("mtime_ns")) == cur["mtime_ns"]
    except Exception:
        return False
