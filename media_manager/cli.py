from __future__ import annotations

import argparse
import getpass
import importlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import sys
import time
import urllib.request
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from . import __version__
from .checkpoints import CheckpointStore
from .config import LibraryConfig
from .database import Database
from .executor import _stage_needs_work, recover_reentrant_state, scan_and_run
from .models import StageStatus, VisualType
from .scanner import scan_library
from .subtitles import OpenSubtitlesClient
from .utils import MediaManagerError


def _root() -> Path:
    return Path.cwd().resolve()


def _warn_secret_flag(args) -> None:
    if getattr(args, "os_api_key", None):
        print(
            "warning: --os-api-key is visible in process listings/shell history; "
            "prefer 'media-manager config opensubtitles' or OPENSUBTITLES_API_KEY",
            file=sys.stderr,
        )


def _redacted_command(argv: list[str]) -> str:
    out: list[str] = []
    redact_next = False
    secret_flags = {"--os-api-key", "--opensubtitles-api-key"}
    for arg in argv:
        if redact_next:
            out.append("<redacted>"); redact_next = False; continue
        if arg in secret_flags:
            out.append(arg); redact_next = True; continue
        if any(arg.startswith(flag + "=") for flag in secret_flags):
            out.append(arg.split("=", 1)[0] + "=<redacted>")
        else:
            out.append(arg)
    return " ".join(out)


def _ensure_config(args, *, auto_init: bool, prompt_opensubtitles: bool = False) -> LibraryConfig:
    _warn_secret_flag(args)
    root = _root()
    created = False
    if LibraryConfig.exists(root):
        cfg = LibraryConfig.load(root)
    elif auto_init:
        cfg = LibraryConfig.init(
            root,
            os_user=getattr(args, "os_user", None),
            os_api_key=getattr(args, "os_api_key", None),
        )
        created = True
        print(f"Initialized media-manager library: {root}")
        print(f"  Movies:   {cfg.movies_root}")
        print(f"  TV Shows: {cfg.tv_root}")
    else:
        raise MediaManagerError(f"No .media-manager/config.toml in {root}; run 'media-manager init' first")

    user = getattr(args, "os_user", None)
    key = getattr(args, "os_api_key", None)
    if user is not None or key is not None:
        cfg.set_opensubtitles(user, key)

    if prompt_opensubtitles and cfg["subtitles"].get("enabled", True):
        user = str(cfg["opensubtitles"].get("username") or "")
        key = str(cfg["opensubtitles"].get("api_key") or "")
        if not user:
            env_user = os.environ.get("OPENSUBTITLES_USERNAME")
            if env_user:
                user = env_user
            elif sys.stdin.isatty():
                user = input("OpenSubtitles username (blank to skip provider setup): ").strip()
        if user and not key:
            env_key = os.environ.get("OPENSUBTITLES_API_KEY")
            if env_key:
                key = env_key
            elif sys.stdin.isatty():
                key = getpass.getpass("OpenSubtitles API key: ").strip()
        if user and key and (
            user != cfg["opensubtitles"].get("username") or key != cfg["opensubtitles"].get("api_key")
        ):
            cfg.set_opensubtitles(user, key)
            print("Stored OpenSubtitles username/API key in .media-manager/config.toml (mode 600).")
    return cfg


def _client_config(cfg: LibraryConfig, args) -> dict:
    data = deepcopy(cfg["opensubtitles"])
    if os.environ.get("OPENSUBTITLES_USERNAME"):
        data["username"] = os.environ["OPENSUBTITLES_USERNAME"]
    if os.environ.get("OPENSUBTITLES_API_KEY"):
        data["api_key"] = os.environ["OPENSUBTITLES_API_KEY"]
    if getattr(args, "os_user", None):
        data["username"] = args.os_user
    if getattr(args, "os_api_key", None):
        data["api_key"] = args.os_api_key
    return data


def _db(cfg: LibraryConfig) -> Database:
    return Database(cfg.state_dir / "media-manager.db")


@contextmanager
def _library_lock(cfg: LibraryConfig):
    # Linux/POSIX advisory lock prevents two processors from mutating the same
    # library simultaneously. Status/pause commands deliberately do not take it.
    import fcntl

    path = cfg.state_dir / "manager.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MediaManagerError(
                "Another media-manager run is active for this library. "
                "Use 'media-manager status' or 'media-manager pause'."
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def cmd_init(args) -> int:
    _warn_secret_flag(args)
    root = _root()
    if LibraryConfig.exists(root):
        cfg = LibraryConfig.load(root)
        if args.os_user is not None or args.os_api_key is not None:
            cfg.set_opensubtitles(args.os_user, args.os_api_key)
        print(f"Already initialized: {cfg.state_dir}")
        return 0
    cfg = LibraryConfig.init(root, os_user=args.os_user, os_api_key=args.os_api_key)
    print(f"Initialized: {cfg.state_dir}")
    print(f"Config:      {cfg.path}")
    print(f"Database:    {cfg.state_dir / 'media-manager.db'}")
    return 0


def cmd_scan(args) -> int:
    cfg = _ensure_config(args, auto_init=True)
    with _library_lock(cfg):
        with _db(cfg) as db:
            recover_reentrant_state(cfg, db)
            stats = scan_library(cfg, db, verbose=args.verbose)
    print(
        f"Discovered {stats.discovered}: {stats.new} new, {stats.changed} changed, "
        f"{stats.unchanged} unchanged, {stats.moved} moved/reconnected, "
        f"{stats.missing} missing historical, {stats.conflicts} conflicts, {stats.failures} failures"
    )
    return 0 if not stats.failures else 1


def _pending_plan(cfg: LibraryConfig, db: Database):
    languages = [str(x) for x in cfg["subtitles"].get("languages", ["en"])]
    for row in db.rows("present=1"):
        stages = db.stages(int(row["id"]))
        todo = []
        for stage in ["visual_type", "av_sync", "crop"] + [f"subtitle:{x}" for x in languages] + ["final_verify"]:
            if stage == "final_verify":
                if stage not in stages or stages[stage]["status"] in {"pending", "unknown", "running"}:
                    todo.append(stage)
            elif _stage_needs_work(stage, stages.get(stage)):
                todo.append(stage)
        if todo:
            yield row, todo


def cmd_plan(args) -> int:
    cfg = _ensure_config(args, auto_init=True)
    with _library_lock(cfg):
        with _db(cfg) as db:
            recover_reentrant_state(cfg, db)
            stats = scan_library(cfg, db)
            plans = list(_pending_plan(cfg, db))
    print(f"Library: {cfg.root}")
    print(f"Videos discovered: {stats.discovered}")
    if not plans:
        print("Nothing to do.")
        return 0
    for i, (row, todo) in enumerate(plans[: args.limit if args.limit else None], 1):
        print(f"{i:4d}. {row['rel_path']}")
        print("      " + " -> ".join(todo))
    return 0


def _reviewed_selection(
    db: Database,
    *,
    stage: str | None = None,
    limit: int | None = None,
    media_id: int | None = None,
) -> dict[int, list[str]]:
    params: list[object] = []
    stage_clause = ""
    if stage:
        stage_clause = " AND s.stage=?"
        params.append(stage)
    if media_id is not None:
        stage_clause += " AND m.id=?"
        params.append(int(media_id))
    rows = db.conn.execute(
        """SELECT m.id,m.rel_path,s.stage FROM stages s
           JOIN media m ON m.id=s.media_id
           WHERE m.present=1 AND s.status IN ('needs_review','insufficient_evidence','failed')"""
        + stage_clause + " ORDER BY m.rel_path,s.stage",
        params,
    ).fetchall()
    selected: dict[int, list[str]] = {}
    for row in rows:
        media_id = int(row["id"])
        if limit is not None and media_id not in selected and len(selected) >= limit:
            continue
        selected.setdefault(media_id, []).append(str(row["stage"]))
    return selected


def _print_reviewed_plan(cfg: LibraryConfig, args) -> int:
    with _db(cfg) as db:
        selected = _reviewed_selection(
            db,
            stage=getattr(args, "review_stage", None),
            limit=getattr(args, "limit", None),
            media_id=getattr(args, "media_id", None),
        )
        print(f"Library: {cfg.root}")
        print("Mode: reviewed-only (no filesystem/library scan)")
        if not selected:
            print("No matching review stages.")
            return 0
        for index, (media_id, stages) in enumerate(selected.items(), 1):
            row = db.get_media(media_id)
            print(f"{index:4d}. {row['rel_path']}")
            print("      " + " -> ".join(stages))
        print(f"Would retry {sum(len(value) for value in selected.values())} stage(s) on {len(selected)} media item(s).")
    return 0


def cmd_run(args) -> int:
    cfg = _ensure_config(
        args,
        auto_init=True,
        prompt_opensubtitles=not bool(getattr(args, "dry_run", False)),
    )
    reviewed_only = bool(getattr(args, "reviewed_only", False))
    due_only = bool(getattr(args, "due_subtitles_only", False))
    if due_only and (reviewed_only or getattr(args, "retry_subtitles_now", False)):
        raise MediaManagerError("--due-subtitles-only cannot be combined with reviewed-only or forced subtitle retry")
    if getattr(args, "dry_run", False):
        return _print_reviewed_plan(cfg, args) if reviewed_only else cmd_plan(args)
    cfg.data.setdefault("_runtime", {})
    cfg.data["_runtime"]["skip_av_sync"] = bool(args.no_av_sync)
    cfg.data["_runtime"]["skip_crop"] = bool(args.no_crop)
    cfg.data["_runtime"]["skip_subtitles"] = bool(args.no_subtitles)
    cfg.data["subtitles"]["force_retry"] = bool(getattr(args, "retry_subtitles_now", False))
    if args.subtitle_language:
        cfg.data["subtitles"]["languages"] = [x.strip() for x in args.subtitle_language.split(",") if x.strip()]

    oscfg = _client_config(cfg, args)
    client = OpenSubtitlesClient(
        oscfg,
        password=cfg.get_opensubtitles_password(),
        verbose=args.verbose,
    ) if (cfg["subtitles"].get("enabled", True) and not cfg.data.get("_runtime", {}).get("skip_subtitles")) else None
    with _library_lock(cfg):
        with _db(cfg) as db:
            if getattr(args, "retry_subtitles_now", False):
                db.conn.execute("UPDATE stages SET status='pending' WHERE stage LIKE 'subtitle:%' AND status='waiting'")
                db.conn.commit()
            recovered = recover_reentrant_state(cfg, db)
            if any(recovered.values()) and args.verbose:
                print(
                    "Recovered prior state: "
                    + ", ".join(f"{k}={v}" for k, v in recovered.items() if v)
                )
            selected_ids: list[int] | None = None
            if due_only:
                from .retry_policy import eligible
                selection = {}
                for item in db.conn.execute("SELECT media_id,stage,details_json FROM stages WHERE status='waiting' AND stage LIKE 'subtitle:%'"):
                    if eligible(json.loads(item["details_json"] or "{}")):
                        selection.setdefault(str(item["media_id"]), []).append(item["stage"])
                cfg.data["_runtime"]["selected_stages"] = selection
                selected_ids = [int(key) for key in selection]
            if reviewed_only:
                selection = _reviewed_selection(
                    db,
                    stage=getattr(args, "review_stage", None),
                    limit=args.limit,
                    media_id=getattr(args, "media_id", None),
                )
                prepared, stage_count = db.prepare_reviewed_retry(
                    media_ids=selection.keys(), stage=getattr(args, "review_stage", None)
                )
                # Keep the original snapshot authoritative: no new review that
                # appears during this run is silently pulled into the batch.
                prepared = {
                    media_id: [stage for stage in stages if stage in selection.get(media_id, [])]
                    for media_id, stages in prepared.items()
                    if media_id in selection
                }
                cfg.data["_runtime"]["selected_stages"] = {
                    str(media_id): stages for media_id, stages in prepared.items()
                }
                selected_ids = list(prepared)
                if args.verbose:
                    print(f"Prepared {stage_count} reviewed stage(s); full library scan disabled.")
            run_id = db.start_run(
                pid=os.getpid(),
                hostname=socket.gethostname(),
                command=_redacted_command(sys.argv),
            )
            try:
                counts = scan_and_run(
                    cfg,
                    db,
                    limit=None if reviewed_only else args.limit,
                    verbose=args.verbose,
                    client=client,
                    progress_enabled=not args.no_progress,
                    scan=not (reviewed_only or due_only),
                    media_ids=selected_ids,
                )
                remaining = client.remaining_downloads if client else None
            except KeyboardInterrupt:
                db.finish_run(run_id, "paused", "Interrupted by SIGINT/Ctrl+C; resumable checkpoints preserved")
                raise
            except Exception as exc:
                db.finish_run(run_id, "failed", str(exc))
                raise
            else:
                db.finish_run(run_id, "completed")
    if not counts:
        print("Nothing to do.")
        return 0
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("needs_review") or counts.get("failed"):
        with _db(cfg) as db:
            problems = db.rows("present=1 AND overall_status IN ('needs_review','failed')")[:8]
            if problems:
                print("Needs attention:")
                for row in problems:
                    reason = row["last_error"] or ""
                    if not reason:
                        for name, st in db.stages(int(row["id"])).items():
                            if st["status"] in {"needs_review", "failed", "insufficient_evidence"}:
                                d = json.loads(st["details_json"] or "{}")
                                reason = d.get("reason") or d.get("error") or d.get("correction_error") or st["status"]
                                correction_error = d.get("correction_error") or d.get("error")
                                if correction_error and correction_error != reason:
                                    reason = f"{name}: {reason} — {correction_error}"
                                else:
                                    reason = f"{name}: {reason}"
                                break
                    print(f"  {row['rel_path']}: {reason}")
    if remaining is not None:
        print(f"OpenSubtitles downloads remaining: {remaining}")
    return 1 if counts.get("failed") else 0


def _proc_state(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(errors="replace")
        close = raw.rfind(")")
        fields = raw[close + 2:].split() if close >= 0 else []
        return fields[0] if fields else None
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    state = _proc_state(pid)
    return state is not None and state != "Z"


def _pid_matches_library(pid: int, cfg: LibraryConfig) -> bool:
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ")
        cwd = Path(f"/proc/{int(pid)}/cwd").resolve()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return False
    return (b"media-manager" in cmdline or b"media_manager" in cmdline) and cwd == cfg.root


def _descendant_pids(root_pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text(errors="replace")
            close = raw.rfind(")")
            fields = raw[close + 2:].split()
            pid = int(stat_path.parent.name)
            ppid = int(fields[1])
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    ordered: list[int] = []

    def visit(parent: int) -> None:
        for child in children.get(parent, []):
            visit(child)
            ordered.append(child)

    visit(int(root_pid))
    return ordered


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _terminate_known_pids(pids: list[int]) -> None:
    live = [pid for pid in pids if _pid_alive(pid)]
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3.0
    while any(_pid_alive(pid) for pid in live) and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in live:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _terminate_process_tree(root_pid: int, known_descendants: list[int] | None = None) -> None:
    descendants = list(known_descendants or _descendant_pids(root_pid))
    _terminate_known_pids(descendants + [int(root_pid)])


def cmd_pause(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    with _db(cfg) as db:
        row = db.active_run()
        if row is None:
            print("No active media-manager run for this library.")
            return 0
        pid = int(row["pid"] or 0)
        run_id = int(row["id"])
        host = str(row["hostname"] or "")
        if host and host != socket.gethostname():
            raise MediaManagerError(f"Active run belongs to host {host}; pause it on that host")
        if pid <= 0:
            db.finish_run(int(row["id"]), "interrupted", "Active run record had no valid PID")
            print("No live process found; stale run state marked interrupted.")
            return 0
        if not _pid_alive(pid):
            db.finish_run(int(row["id"]), "interrupted", "Process no longer exists")
            print("The recorded process is no longer running; state is already resumable.")
            return 0
        if not _pid_matches_library(pid, cfg):
            db.finish_run(run_id, "interrupted", "Recorded PID no longer belongs to this library run")
            raise MediaManagerError(
                f"Refusing to signal PID {pid}: it no longer matches this media-manager library run"
            )
        descendants = _descendant_pids(pid)
        os.kill(pid, signal.SIGINT)
        print(f"Pause requested for media-manager PID {pid}.")
        print("Waiting for its current operation to stop and release the library lock...")

    timeout = max(0.0, float(getattr(args, "timeout", 30.0)))
    if not _wait_for_process_exit(pid, timeout):
        if not bool(getattr(args, "force", False)):
            print(
                f"PID {pid} did not stop within {timeout:g} seconds. It is still the active owner; "
                "retry with 'media-manager pause --force' to terminate it and recover the last checkpoint.",
                file=sys.stderr,
            )
            return 1
        print(f"PID {pid} did not stop cleanly; terminating its process tree.", file=sys.stderr)
        _terminate_process_tree(pid, descendants)
        if not _wait_for_process_exit(pid, 10.0):
            raise MediaManagerError(f"Could not terminate media-manager PID {pid}")

    # A parent can exit while a misbehaving external tool ignores the signal.
    # Such children no longer own valid work and must not continue burning CPU.
    orphaned = [child for child in descendants if _pid_alive(child)]
    if orphaned:
        _terminate_known_pids(orphaned)
        print(f"Stopped {len(orphaned)} orphaned child process(es).", file=sys.stderr)

    recovered = False
    with _library_lock(cfg):
        with _db(cfg) as db:
            active = db.active_run()
            if active is not None and int(active["id"]) == run_id:
                recover_reentrant_state(cfg, db)
                recovered = True
    if recovered:
        print("Stopped uncleanly; running state was recovered to the last durable checkpoint.")
    else:
        print("Stopped cleanly; checkpoints are preserved.")
    print("Resume with 'media-manager resume'.")
    return 0


def cmd_status(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    with _db(cfg) as db:
        rows = db.rows("present=1")
        missing = len(db.rows("present=0"))
        totals: dict[str, int] = {}
        movie = tv = 0
        for r in rows:
            totals[r["overall_status"]] = totals.get(r["overall_status"], 0) + 1
            if r["media_type"] == "movie": movie += 1
            else: tv += 1
        print(f"Library: {cfg.root}")
        print(f"Media present: {len(rows)} ({movie} movies, {tv} episodes); historical missing: {missing}")
        latest = db.latest_run()
        if latest is not None:
            print(f"Last run: {latest['status']} (started {latest['started_at']})")
            if latest['status'] == "running" and latest['pid']:
                pid = int(latest['pid'])
                alive = True
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    alive = False
                if alive:
                    proc_state = ""
                    try:
                        for line in Path(f"/proc/{pid}/status").read_text(errors="replace").splitlines():
                            if line.startswith("State:"):
                                proc_state = line.split(":", 1)[1].strip(); break
                    except Exception:
                        pass
                    print(f"  active PID: {pid}" + (f" ({proc_state})" if proc_state else ""))
                else:
                    print(f"  stale run record: PID {pid} no longer exists; next run/resume will recover checkpoints")
            if latest['reason']:
                print(f"  {latest['reason']}")
        for key in sorted(totals):
            print(f"  {key:18s} {totals[key]}")
        if args.stages:
            print("\nStages:")
            for r in db.stage_counts():
                print(f"  {r['stage']:20s} {r['status']:24s} {r['n']}")
            active = db.conn.execute(
                """SELECT m.rel_path,s.stage,s.details_json FROM stages s JOIN media m ON m.id=s.media_id WHERE s.status='running' ORDER BY s.updated_at DESC"""
            ).fetchall()
            for a in active:
                d = json.loads(a["details_json"] or "{}")
                suffix = ""
                if a["stage"] == "crop" and d.get("total_chunks"):
                    c, t = float(d.get("completed_chunks", 0)), float(d.get("total_chunks", 0))
                    if d.get("chunk_frames"):
                        c += float(d.get("chunk_frame", 0)) / max(float(d.get("chunk_frames", 1)), 1.0)
                    suffix = f" {c:.1f}/{int(t)} chunks ({100*c/t:.1f}%)" if t else ""
                elif d.get("total"):
                    c, t = float(d.get("completed",0)), float(d.get("total",0))
                    suffix = f" {int(c)}/{int(t)} ({100*c/t:.1f}%)" if t else ""
                print(f"    -> {a['rel_path']}: {a['stage']} {d.get('phase','')}{suffix}")
    return 0


def _progress_snapshot(cfg: LibraryConfig) -> str:
    with _db(cfg) as db:
        run_row = db.active_run()
        active = db.conn.execute(
            """SELECT m.rel_path,s.stage,s.status,s.details_json,s.updated_at FROM stages s JOIN media m ON m.id=s.media_id WHERE s.status='running' ORDER BY s.updated_at DESC LIMIT 1"""
        ).fetchone()
        lines = [f"Library: {cfg.root}"]
        if run_row is not None:
            lines.append(f"Run: PID {run_row['pid']} on {run_row['hostname']} since {run_row['started_at']}")
        if active is None:
            lines.append("No stage is currently marked running.")
            return "\n".join(lines)
        d = json.loads(active["details_json"] or "{}")
        lines += [f"Media: {active['rel_path']}", f"Stage: {active['stage']} / {d.get('phase','working')}"]
        if active["stage"] == "crop" and d.get("total_chunks"):
            c, t = float(d.get("completed_chunks",0)), float(d.get("total_chunks",0))
            if d.get("chunk_frames"):
                c += float(d.get("chunk_frame",0))/max(float(d.get("chunk_frames",1)),1)
            lines.append(f"Progress: {c:.1f}/{int(t)} chunks ({100*c/t:.1f}%)")
            if d.get("valid_frames") is not None:
                lines.append(f"Native samples checkpointed: {d.get('valid_frames')}")
        elif d.get("total"):
            c,t=float(d.get("completed",0)),float(d.get("total",0)); lines.append(f"Progress: {int(c)}/{int(t)} ({100*c/t:.1f}%)" if t else "Progress: working")
        if d.get("checkpoint_path"):
            lines.append(f"Checkpoint: {d['checkpoint_path']}")
        return "\n".join(lines)


def cmd_progress(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    if not args.watch:
        print(_progress_snapshot(cfg)); return 0
    try:
        while True:
            text = _progress_snapshot(cfg)
            sys.stdout.write("\x1b[2J\x1b[H" + text + "\n")
            sys.stdout.flush(); time.sleep(max(0.5, float(args.interval)))
    except KeyboardInterrupt:
        return 0


def cmd_review(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    with _db(cfg) as db:
        params: list[object] = []
        stage_filter = ""
        if getattr(args, "stage", None):
            stage_filter = " AND s.stage=?"
            params.append(args.stage)
        problem_rows = db.conn.execute(
            """SELECT m.id,m.rel_path,m.overall_status,m.last_error,s.stage,s.status,s.details_json
               FROM media m JOIN stages s ON s.media_id=m.id
               WHERE m.present=1 AND s.status IN ('needs_review','insufficient_evidence','failed')"""
            + stage_filter + " ORDER BY m.rel_path,s.stage",
            params,
        ).fetchall()
        if not problem_rows:
            print("Review queue is empty.")
            return 0
        items = []
        for row in problem_rows:
            details = json.loads(row["details_json"] or "{}")
            items.append({
                "media_id": int(row["id"]), "path": row["rel_path"],
                "overall_status": row["overall_status"], "stage": row["stage"],
                "status": row["status"],
                "reason": details.get("reason") or details.get("error") or details.get("correction_error") or row["last_error"] or "",
            })
        if getattr(args, "json", False):
            print(json.dumps(items, indent=2, ensure_ascii=False)); return 0
        if getattr(args, "summary", False):
            counts: dict[tuple[str, str], int] = {}
            for item in items:
                key = (str(item["stage"]), str(item["status"]))
                counts[key] = counts.get(key, 0) + 1
            print(f"Review stages: {len(items)} across {len({item['media_id'] for item in items})} media item(s)")
            for (stage, status), count in sorted(counts.items()):
                print(f"  {stage:20s} {status:24s} {count}")
            return 0
        current_id = None
        for item in items:
            if item["media_id"] != current_id:
                current_id = item["media_id"]
                print(f"\n{item['path']}  [{item['overall_status']}]")
            print(f"  {item['stage']}: {item['status']} {item['reason']}")
    return 0


def cmd_history(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    with _db(cfg) as db:
        if args.path:
            row = db.find_media_by_path(args.path, cfg.root)
            if row is None:
                raise MediaManagerError(f"Not found in database: {args.path}")
            events = db.conn.execute(
                "SELECT created_at,kind,message,details_json FROM events WHERE media_id=? ORDER BY id DESC LIMIT ?",
                (int(row["id"]), int(args.limit)),
            ).fetchall()
            print(f"History: {row['rel_path']}")
            for event in events:
                print(f"{event['created_at']}  {event['kind']}: {event['message']}")
        else:
            runs = db.conn.execute(
                "SELECT id,started_at,ended_at,status,pid,reason FROM runs ORDER BY id DESC LIMIT ?",
                (int(args.limit),),
            ).fetchall()
            for run in runs:
                suffix = f" — {run['reason']}" if run["reason"] else ""
                print(f"#{run['id']} {run['started_at']}  {run['status']}  ended={run['ended_at'] or '-'}{suffix}")
    return 0


def cmd_help(args) -> int:
    parser = args.root_parser
    if not args.topic:
        parser.print_help(); return 0
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    command = subparsers.choices.get(args.topic)
    if command is None:
        raise MediaManagerError(f"Unknown command: {args.topic}")
    command.print_help()
    return 0


def cmd_explain(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    with _db(cfg) as db:
        row = db.find_media_by_path(args.path, cfg.root)
        if row is None:
            raise MediaManagerError(f"Not found in database: {args.path}")
        print(f"Path:        {row['rel_path']}")
        print(f"Type:        {row['media_type']}")
        print(f"Identity:    {row['title']}" + (f" ({row['year']})" if row['year'] else ""))
        if row["media_type"] == "episode":
            print(f"Episode:     s{int(row['season']):02d}e{int(row['episode']):02d} {row['episode_title'] or ''}")
        print(f"Fingerprint: {row['fingerprint']}")
        print(f"Status:      {row['overall_status']}")
        print(f"Visual type: {row['visual_type']}")
        print("\nStages:")
        for name, stage in db.stages(int(row["id"])).items():
            print(f"\n[{name}] {stage['status']}")
            details = json.loads(stage["details_json"] or "{}")
            if name == "crop":
                print(f"  algorithm: {details.get('algorithm_version') or 'legacy'}")
                print(f"  source:    {details.get('source_width', row['width'])}x{details.get('source_height', row['height'])} / {details.get('method','legacy')}")
                print(
                    "  verified:  "
                    f"L{details.get('verified_left', details.get('safe_left',0))} "
                    f"R{details.get('verified_right', details.get('safe_right',0))} "
                    f"T{details.get('verified_top', details.get('safe_top',0))} "
                    f"B{details.get('verified_bottom', details.get('safe_bottom',0))}"
                )
                print(
                    "  applied:   "
                    f"L{details.get('safe_left',0)} R{details.get('safe_right',0)} "
                    f"T{details.get('safe_top',0)} B{details.get('safe_bottom',0)}"
                )
                print(f"  variable:  {details.get('variable')} meaningful_states={len(details.get('meaningful_states') or {})} raw_states={len(details.get('raw_states') or details.get('states') or {})}")
                if details.get("reason"):
                    print(f"  reason:    {details['reason']}")
            elif name == "av_sync":
                print(f"  algorithm: {details.get('algorithm_version') or 'legacy'}")
                print(f"  observations: {len(details.get('observations') or [])}; median={details.get('median_offset_ms')} ms; spread={details.get('spread_ms')} ms; drift={details.get('drift_ms_per_hour')} ms/hour")
                diag = details.get("diagnostics") or {}
                if diag:
                    print(
                        "  SyncNet:     "
                        f"tested={diag.get('tested',0)} usable={diag.get('successful',0)} "
                        f"failed={diag.get('failed',0)} no_face={diag.get('no_face',0)} "
                        f"low={diag.get('low_confidence',0)} "
                        f"high={diag.get('high_confidence',0)} threshold={diag.get('confidence_threshold')} "
                        f"best={diag.get('best_confidence')} median={diag.get('median_confidence')}"
                    )
                    if details.get("classification") or diag.get("classification"):
                        print(f"  classification: {details.get('classification') or diag.get('classification')}")
                    breaker = diag.get("circuit_breaker") or {}
                    if breaker.get("triggered"):
                        print(
                            "  circuit:      "
                            f"stopped after {breaker.get('consecutive')}/{breaker.get('threshold')} "
                            f"identical {breaker.get('phase')} failures; skipped={breaker.get('skipped_candidates')}"
                        )
                    for failure in diag.get("failure_groups") or []:
                        print(
                            "  failure group: "
                            f"count={failure.get('count')} phase={failure.get('phase')} "
                            f"type={failure.get('exception_type')} message={failure.get('message')}"
                        )
                        representative = str(failure.get("traceback") or "").rstrip()
                        if representative:
                            print("  representative traceback:")
                            for line in representative.splitlines():
                                print(f"    {line}")
                if details.get("reason"):
                    print(f"  reason: {details['reason']}")
                if details.get("correction_error") or details.get("error"):
                    print(f"  correction error: {details.get('correction_error') or details.get('error')}")
                if details.get("analysis_checkpoint_preserved"):
                    print("  checkpoint: completed A/V analysis preserved for deliberate retry")
            elif name.startswith("subtitle:"):
                ff = details.get("ffsubsync") or {}
                adj = details.get("adjustment_validation") or {}
                print(f"  severity:  {details.get('severity','legacy')}")
                if ff.get("sync_applied") is False or ff.get("quality_rejected"):
                    print(
                        "  ffsubsync: applied=no; "
                        f"proposed offset={ff.get('offset_seconds')}s "
                        f"scale={ff.get('scale')} score={ff.get('score')}"
                    )
                else:
                    print(f"  ffsubsync: offset={ff.get('offset_seconds')}s scale={ff.get('scale')} score={ff.get('score')}")
                if adj.get("timing_conversion"):
                    print(f"  timing:    {adj['timing_conversion']}")
                if details.get("reason"):
                    print(f"  reason:    {details['reason']}")
            print(json.dumps(details, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_retry(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    if getattr(args, "stage", None):
        with _db(cfg) as db:
            params: list[object] = [args.stage]
            where = "stage=?"
            if getattr(args, "path", None):
                row = db.find_media_by_path(args.path, cfg.root)
                if row is None:
                    raise MediaManagerError(f"Not found in database: {args.path}")
                where += " AND media_id=?"; params.append(int(row["id"]))
            ids = [int(r["media_id"]) for r in db.conn.execute(f"SELECT DISTINCT media_id FROM stages WHERE {where}", params).fetchall()]
            cur = db.conn.execute(f"DELETE FROM stages WHERE {where}", params)
            for media_id in ids:
                CheckpointStore(cfg.state_dir, media_id).clear(args.stage)
                db.conn.execute("UPDATE media SET overall_status='incomplete',last_error=NULL WHERE id=?", (media_id,))
            db.conn.commit()
        print(f"Reset {cur.rowcount} '{args.stage}' stage record(s). Unrelated stages were preserved.")
        return 0
    statuses = set()
    if args.failed: statuses.add("failed")
    if args.review:
        statuses.update({"needs_review", "insufficient_evidence"})
    if args.unavailable: statuses.add("unavailable")
    if not statuses:
        statuses.update({"failed", "needs_review", "insufficient_evidence"})
    with _db(cfg) as db:
        n = db.reset_stages(statuses)
    print(f"Reset {n} stage record(s). Run 'media-manager run' to retry them.")
    return 0


def cmd_config(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    if getattr(args, "section", None) == "opensubtitles":
        current_user = str(cfg["opensubtitles"].get("username") or "")
        prompt = f"OpenSubtitles username [{current_user}]: " if current_user else "OpenSubtitles username: "
        entered = input(prompt).strip() if sys.stdin.isatty() else ""
        username = entered or current_user
        current_key = str(cfg["opensubtitles"].get("api_key") or "")
        key_prompt = "OpenSubtitles API key (blank keeps current): " if current_key else "OpenSubtitles API key: "
        api_key = getpass.getpass(key_prompt).strip() if sys.stdin.isatty() else ""
        api_key = api_key or current_key
        current_password = cfg.get_opensubtitles_password()
        password_prompt = (
            "OpenSubtitles password (blank keeps stored password): "
            if current_password else "OpenSubtitles password: "
        )
        entered_password = getpass.getpass(password_prompt) if sys.stdin.isatty() else ""
        password = entered_password or current_password or os.environ.get("OPENSUBTITLES_PASSWORD")
        if not username or not api_key or not password:
            raise MediaManagerError(
                "OpenSubtitles username, API key, and password are required for stored provider configuration"
            )
        cfg.set_opensubtitles(username, api_key)
        cfg.set_opensubtitles_password(password)
        print("OpenSubtitles username/API key saved in .media-manager/config.toml (mode 600).")
        print(f"OpenSubtitles password saved in {cfg.opensubtitles_password_path} (mode 600).")
        return 0
    if args.os_user is not None or args.os_api_key is not None:
        cfg.set_opensubtitles(args.os_user, args.os_api_key)
        print("Configuration updated.")
    data = deepcopy(cfg.data)
    if data["opensubtitles"].get("api_key"):
        data["opensubtitles"]["api_key"] = "<configured>"
    data["opensubtitles"]["password"] = (
        "<stored separately>" if cfg.get_opensubtitles_password() else "<not stored>"
    )
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _download(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    if tmp.exists(): tmp.unlink()
    print(f"Downloading {dst.name}...")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    if tmp.stat().st_size < 100_000:
        tmp.unlink(missing_ok=True)
        raise MediaManagerError(f"Downloaded weight file is unexpectedly small: {dst.name}")
    tmp.replace(dst)


def cmd_setup_syncnet(args) -> int:
    cfg = _ensure_config(args, auto_init=True)
    s3fd = cfg.resolve(cfg["av_sync"]["syncnet_s3fd_weights"])
    model = cfg.resolve(cfg["av_sync"]["syncnet_weights"])
    base = "https://raw.githubusercontent.com/nawta/SyncNet_py309_313/main/weights"
    if args.force or not s3fd.exists():
        _download(base + "/sfd_face.pth", s3fd)
    else:
        print(f"Already exists: {s3fd}")
    if args.force or not model.exists():
        _download(base + "/syncnet_v2.model", model)
    else:
        print(f"Already exists: {model}")
    print("Weights ready. Install the optional A/V dependencies with:")
    print("  pip install -e '.[avsync]'  # includes the SyncNet-compatible PySceneDetect range")
    return 0


def cmd_doctor(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    checks = []
    for tool in ("ffmpeg", "ffprobe", "ffsubsync"):
        checks.append((tool, bool(shutil.which(tool)), shutil.which(tool) or "missing"))
    if not shutil.which("ffsubsync"):
        checks.append(("ffs", bool(shutil.which("ffs")), shutil.which("ffs") or "missing"))
    for mod in ("cv2", "silero_vad"):
        checks.append((f"python:{mod}", importlib.util.find_spec(mod) is not None, "installed" if importlib.util.find_spec(mod) else "missing"))
    syncnet_spec = importlib.util.find_spec("syncnet_python")
    if syncnet_spec is None:
        checks.append(("python:syncnet_python", False, "missing"))
    else:
        try:
            syncnet_module = importlib.import_module("syncnet_python")
            pipeline_class = getattr(syncnet_module, "SyncNetPipeline", None)
            if not callable(pipeline_class):
                # syncnet-python 0.2.2 suppresses its own internal ImportError;
                # import the implementation directly so doctor shows the cause.
                direct = importlib.import_module("syncnet_python.syncnet_pipeline")
                pipeline_class = getattr(direct, "SyncNetPipeline", None)
            if not callable(pipeline_class):
                raise TypeError("SyncNetPipeline export is not callable")
            checks.append(("python:syncnet_python", True, f"callable ({syncnet_spec.origin})"))
        except Exception as exc:
            checks.append(("python:syncnet_python", False, f"unusable: {type(exc).__name__}: {exc}"))
    for key in ("syncnet_s3fd_weights", "syncnet_weights"):
        p = cfg.resolve(cfg["av_sync"][key])
        checks.append((key, p.is_file() and p.stat().st_size > 0, str(p)))
    checks.append(("OpenSubtitles username", bool(cfg["opensubtitles"].get("username")), "configured" if cfg["opensubtitles"].get("username") else "missing"))
    checks.append(("OpenSubtitles API key", bool(cfg["opensubtitles"].get("api_key")), "configured" if cfg["opensubtitles"].get("api_key") else "missing"))
    stored_password = cfg.get_opensubtitles_password()
    if os.environ.get("OPENSUBTITLES_TOKEN"):
        password_detail = "token supplied by environment"
    elif os.environ.get("OPENSUBTITLES_PASSWORD"):
        password_detail = "supplied by environment"
    elif stored_password:
        password_detail = f"stored in {cfg.opensubtitles_password_path} (mode 600)"
    else:
        password_detail = "missing; an interactive run will prompt when a download is needed"
    checks.append((
        "OpenSubtitles password/token",
        bool(os.environ.get("OPENSUBTITLES_TOKEN") or os.environ.get("OPENSUBTITLES_PASSWORD") or stored_password),
        password_detail,
    ))
    bad = 0
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'MISS':4s}  {name:28s} {detail}")
        if not ok: bad += 1
    print("\nMissing optional A/V components do not prevent crop/subtitle processing; A/V sync will be marked unavailable.")
    return 1 if bad else 0


def cmd_set_visual_type(args) -> int:
    cfg = _ensure_config(args, auto_init=False)
    visual = VisualType(args.type)
    with _db(cfg) as db:
        row = db.find_media_by_path(args.path, cfg.root)
        if row is None:
            raise MediaManagerError(f"Not found in database: {args.path}; run media-manager scan first")
        db.set_override(int(row["id"]), "visual_type", visual.value)
        db.set_visual_type(int(row["id"]), visual, 1.0)
        db.conn.execute("DELETE FROM stages WHERE media_id=? AND stage IN ('visual_type','av_sync')", (row["id"],))
        db.conn.execute("UPDATE media SET overall_status='incomplete' WHERE id=?", (row["id"],))
        db.conn.commit()
    print(f"Set visual type override: {args.path} -> {visual.value}")
    return 0


def cmd_web(args) -> int:
    if getattr(args, "app_state", None):
        state_root = Path(args.app_state).expanduser().resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        cfg = LibraryConfig.load(state_root) if LibraryConfig.exists(state_root) else LibraryConfig.init(state_root)
        from .web import serve
        serve(cfg, host=str(args.host), port=int(args.port),
              token_file=Path(args.token_file).expanduser().resolve() if args.token_file else None,
              standalone=True)
        return 0
    library_root = Path(args.library_root).expanduser().resolve()
    if not library_root.is_dir():
        raise MediaManagerError(f"Library root is not a directory: {library_root}")
    os.chdir(library_root)
    cfg = _ensure_config(args, auto_init=True)
    from .web import serve
    serve(
        cfg,
        host=str(args.host),
        port=int(args.port),
        token_file=Path(args.token_file).expanduser().resolve() if args.token_file else None,
    )
    return 0


def add_os_flags(p):
    p.add_argument("--opensubtitles-username", "--os-user", dest="os_user")
    p.add_argument("--opensubtitles-api-key", "--os-api-key", dest="os_api_key")


def _add_run_flags(q) -> None:
    add_os_flags(q)
    q.add_argument("--limit", type=int)
    q.add_argument("--dry-run", action="store_true", help="Alias for a non-destructive plan")
    q.add_argument("--no-av-sync", action="store_true")
    q.add_argument("--no-crop", action="store_true")
    q.add_argument("--no-subtitles", action="store_true")
    q.add_argument("--retry-subtitles-now", action="store_true", help="Bypass subtitle cooldown/release schedule, retaining candidate validation")
    q.add_argument("--due-subtitles-only", action="store_true", help="Retry only eligible waiting subtitles without a library scan")
    q.add_argument("--subtitle-language", help="Comma-separated languages, e.g. en,ar")
    q.add_argument("-v", "--verbose", action="store_true")
    q.add_argument("--no-progress", action="store_true", help="Disable the live single-line progress display")
    q.add_argument(
        "--reviewed-only",
        action="store_true",
        help="Retry only the current review queue without rescanning the library",
    )
    q.add_argument("--review-stage", help="With --reviewed-only, retry only this stage (for example av_sync)")
    q.add_argument("--media-id", type=int, help="With --reviewed-only, retry only this database media ID")


def cmd_setup_languages(args) -> int:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise MediaManagerError("Install the languages extra first: pipx inject stateful-media-manager faster-whisper") from exc
    cfg = _ensure_config(args, auto_init=False)
    with _library_lock(cfg):
        WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=2)
        cfg["audio_languages"]["model"] = args.model
        cfg.save()
        with _db(cfg) as db:
            # Recheck unavailable language results after explicit model setup.
            db.conn.execute("DELETE FROM stages WHERE stage='audio_languages'")
            db.conn.commit()
    print("Language model cached. Processing will use local files only.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media-manager", description="Stateful media QA and repair manager")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("setup-languages", help="Download/cache the optional local speech-language model")
    q.add_argument("--model", default="tiny")
    q.set_defaults(func=cmd_setup_languages)

    q = sub.add_parser("init", help="Initialize the current directory as a media-manager library")
    add_os_flags(q); q.set_defaults(func=cmd_init)

    q = sub.add_parser("scan", help="Discover/update media state without processing")
    add_os_flags(q); q.add_argument("-v", "--verbose", action="store_true"); q.set_defaults(func=cmd_scan)

    q = sub.add_parser("plan", help="Show pending pipeline work")
    add_os_flags(q); q.add_argument("--limit", type=int); q.set_defaults(func=cmd_plan)

    q = sub.add_parser("run", help="Scan recursively and process unfinished media")
    _add_run_flags(q); q.set_defaults(func=cmd_run)

    q = sub.add_parser("resume", help="Resume the same checkpointed pipeline as 'run'")
    _add_run_flags(q); q.set_defaults(func=cmd_run)

    q = sub.add_parser("rerun-reviewed", help="Retry only reviewed stages; do not rescan the whole library")
    _add_run_flags(q); q.set_defaults(func=cmd_run, reviewed_only=True)

    q = sub.add_parser("pause", help="Gracefully pause the active run for this library")
    q.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for a clean stop (default: 30)")
    q.add_argument("--force", action="store_true", help="Terminate a run that ignores the clean pause request")
    q.set_defaults(func=cmd_pause)

    q = sub.add_parser("status", help="Show library state")
    q.add_argument("--stages", action="store_true"); q.set_defaults(func=cmd_status)
    q = sub.add_parser("progress", help="Show the currently active file/stage and checkpoint progress")
    q.add_argument("--watch", action="store_true"); q.add_argument("--interval", type=float, default=2.0); q.set_defaults(func=cmd_progress)
    q = sub.add_parser("review", help="Show files requiring review")
    q.add_argument("--summary", action="store_true", help="Group review counts by stage/status")
    q.add_argument("--stage", help="Show only one stage")
    q.add_argument("--json", action="store_true", help="Emit machine-readable review records")
    q.set_defaults(func=cmd_review)
    q = sub.add_parser("explain", help="Explain all recorded decisions for one media file")
    q.add_argument("path"); q.set_defaults(func=cmd_explain)
    q = sub.add_parser("retry", help="Reset problem stages or one specific stage without touching unrelated work")
    q.add_argument("path", nargs="?"); q.add_argument("--stage"); q.add_argument("--failed", action="store_true"); q.add_argument("--review", action="store_true"); q.add_argument("--unavailable", action="store_true"); q.set_defaults(func=cmd_retry)
    q = sub.add_parser("config", help="Show/update local library configuration")
    q.add_argument("section", nargs="?", choices=["opensubtitles"]); add_os_flags(q); q.set_defaults(func=cmd_config)
    q = sub.add_parser("doctor", help="Check external/optional dependencies")
    q.set_defaults(func=cmd_doctor)
    q = sub.add_parser("setup-syncnet", help="Download SyncNet model weights into this library")
    q.add_argument("--force", action="store_true"); q.set_defaults(func=cmd_setup_syncnet)
    q = sub.add_parser("set-visual-type", help="Override live_action/animation/mixed/unknown for one media item")
    q.add_argument("path"); q.add_argument("type", choices=[x.value for x in VisualType]); q.set_defaults(func=cmd_set_visual_type)
    q = sub.add_parser("web", help="Run the authenticated local web dashboard")
    q.add_argument("--library-root", default=".", help="Initialized media library (default: current directory)")
    q.add_argument("--app-state", help="Separate dashboard state directory; libraries come from MEDIA_LIBRARIES")
    q.add_argument("--host", default="0.0.0.0", help="Listen on every network interface (default: 0.0.0.0)")
    q.add_argument("--port", type=int, default=8787, help="Listen port (default: 8787)")
    q.add_argument("--token-file", help="Dashboard token file (default: .media-manager/web-token)")
    q.set_defaults(func=cmd_web)
    q = sub.add_parser("history", help="Show recent runs or events for one media file")
    q.add_argument("path", nargs="?"); q.add_argument("--limit", type=int, default=25); q.set_defaults(func=cmd_history)
    q = sub.add_parser("help", help="Show general help or help for one command")
    q.add_argument("topic", nargs="?"); q.set_defaults(func=cmd_help, root_parser=p)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        from .environment import load_environment
        load_environment(getattr(args, "library_root", None))
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nPaused. Stage and intra-stage checkpoint state has been preserved. Resume with 'media-manager resume'.", file=sys.stderr)
        return 130
    except MediaManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback; traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
