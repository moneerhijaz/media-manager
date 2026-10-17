"""One authenticated dashboard, isolated library state, one processing queue."""
import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path

from .config import LibraryConfig
from .utils import MediaManagerError


class LibraryHub:
    def __init__(self, primary, allowed, *, standalone=False):
        self.primary = primary
        self.standalone = standalone
        self.registry = primary.cfg.state_dir / "libraries.json"
        self.allowed = list(dict.fromkeys([*([primary.cfg.root] if not standalone else []), *(Path(p).resolve() for p in allowed)]))
        for i, root in enumerate(self.allowed):
            if not root.is_dir():
                raise MediaManagerError(f"Library mount does not exist: {root}")
            if standalone and (root == primary.cfg.root or root.is_relative_to(primary.cfg.root) or primary.cfg.root.is_relative_to(root)):
                raise MediaManagerError("App state must be separate from media libraries")
            for other in self.allowed[:i]:
                if root.samefile(other) or root.is_relative_to(other) or other.is_relative_to(root):
                    raise MediaManagerError("Library roots must not overlap")
        self.children = {} if standalone else {self.key(primary.cfg.root): primary}
        self.lock = threading.RLock()
        self.worker = None
        self.cancelled = False
        self.pending = []
        self.current = None
        self.last_error = None
        registered = None
        if standalone and self.registry.exists():
            try:
                registered = json.loads(self.registry.read_text())
                if not isinstance(registered, list) or any(not isinstance(p, str) for p in registered):
                    raise ValueError()
            except (ValueError, OSError):
                raise MediaManagerError("Cannot read app library registry") from None
        for root in self.allowed:
            if root != primary.cfg.root and LibraryConfig.exists(root) and (registered is None or str(root) in registered):
                self._attach(root)
        if standalone and registered is None:
            self._save_registry()

    def _save_registry(self):
        if self.standalone:
            from .web import _write_token
            # Retain disconnected registrations; never silently forget a disk.
            previous = json.loads(self.registry.read_text()) if self.registry.exists() else []
            paths = sorted(set(previous) | {str(child.cfg.root) for child in self.children.values()})
            _write_token(self.registry, json.dumps(paths))

    @staticmethod
    def key(root):
        return hashlib.sha256(str(root).encode()).hexdigest()[:16]

    def _attach(self, root):
        from .web import Dashboard
        if LibraryConfig.exists(root):
            cfg = LibraryConfig.load(root)
        else:
            cfg = LibraryConfig.init(root)
            if self.standalone:
                for section in ("processing", "av_sync", "crop", "subtitles", "metadata", "audio_languages"):
                    cfg.data[section] = deepcopy(self.primary.cfg.data[section])
                for name in ("movies_directory", "tv_directory", "extensions"):
                    cfg["library"][name] = deepcopy(self.primary.cfg["library"][name])
                cfg.save()
        child = Dashboard(cfg, self.primary.token, token_path=self.primary.token_path)
        self.children[self.key(root)] = child
        return child

    def target(self, key):
        if not key:
            return self.primary
        if key == "all":
            raise MediaManagerError("Select one library for settings or media details")
        try:
            return self.children[key]
        except KeyError:
            raise MediaManagerError("Unknown library") from None

    def listing(self):
        with self.lock:
            return {"libraries": [{"id": self.key(p), "path": str(p), "registered": self.key(p) in self.children}
                                  for p in self.allowed], "pending": list(self.pending),
                    "current": self.current, "error": self.last_error,
                    "busy": bool(self.worker and self.worker.is_alive())}

    def add(self, payload):
        key = str(payload.get("library_id", ""))
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise MediaManagerError("Pause the queue before adding a library")
            root = next((p for p in self.allowed if self.key(p) == key), None)
            if root is None:
                raise MediaManagerError("Only configured library mounts may be added")
            if key not in self.children:
                self._attach(root)
                self._save_registry()
            return {"message": "Library added", "library_id": key}

    def status(self, key=None):
        if self.standalone and not key:
            key = "all"
        if key != "all":
            result = self.target(key).status()
        else:
            result = {"library": "All libraries", "counts": {}, "reviews": [], "runs": [],
                      "active_run": None, "current_stage": None}
            with self.lock:
                children = list(self.children.items())
            for library_id, child in children:
                data = child.status()
                for status, count in data["counts"].items():
                    result["counts"][status] = result["counts"].get(status, 0) + count
                for name in ("reviews", "runs"):
                    for row in data[name]:
                        row["library_id"] = library_id
                        if name == "reviews":
                            row["path"] = f"{child.cfg.root}: {row['path']}"
                        else:
                            row["reason"] = f"{child.cfg.root}: {row.get('reason') or ''}"
                        result[name].append(row)
                if data["active_run"]:
                    result["active_run"] = data["active_run"]
                    result["current_stage"] = data["current_stage"]
            result["runs"].sort(key=lambda r: r.get("started_at") or "", reverse=True)
        result["queue"] = self.listing()
        return result

    def start(self, payload, key=None):
        if self.standalone and not key:
            key = "all"
        with self.lock:
            if self.worker and self.worker.is_alive() or any(c._active_run() for c in self.children.values()):
                raise MediaManagerError("Another library is processing; pause it first")
            keys = list(self.children) if key == "all" else [self.key(self.target(key).cfg.root)]
            if not keys:
                raise MediaManagerError("Add a media library before starting processing")
            if key == "all" and payload.get("mode") == "single":
                raise MediaManagerError("Select a library for a single-item retry")
            self.cancelled = False
            self.last_error = None
            self.current = keys[0]
            self.pending = keys[1:]
            try:
                result = self.children[self.current].start(payload)
            except Exception:
                self.current = None
                self.pending = []
                raise
            self.worker = threading.Thread(target=self._drain, args=(dict(payload),), daemon=True)
            self.worker.start()
            return {**result, "message": f"Queued {len(keys)} library/libraries sequentially"}

    def _drain(self, payload):
        try:
            while True:
                self.children[self.current]._child.wait()
                with self.lock:
                    if self.cancelled or not self.pending:
                        break
                    if any(child._active_run() for child in self.children.values()):
                        raise MediaManagerError("Queue stopped because another library operation became active")
                    self.current = self.pending.pop(0)
                    self.children[self.current].start(payload)
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
        finally:
            with self.lock:
                self.current = None
                self.pending = []

    def pause(self, key=None):
        if self.standalone and not key:
            key = "all"
        with self.lock:
            self.cancelled = True
            self.pending = []
            if self.current:
                target = self.children[self.current]
            elif key == "all":
                active = [child for child in self.children.values() if child._active_run()]
                if not active:
                    return {"message": "No active run; queue cleared"}
                target = active[-1]
            else:
                target = self.target(key)
            return target.pause()


def configured_libraries():
    import os
    try:
        paths = json.loads(os.environ.get("MEDIA_LIBRARIES", "[]"))
    except ValueError:
        raise MediaManagerError("MEDIA_LIBRARIES must be a JSON array of container paths") from None
    if not isinstance(paths, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
        raise MediaManagerError("MEDIA_LIBRARIES must contain absolute container paths")
    return paths
