from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import MediaIdentity, MediaRecord, MediaType, ProbeInfo, StageStatus, VisualType
from .utils import now_iso


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    rel_path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    show_title TEXT,
    season INTEGER,
    episode INTEGER,
    episode_title TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    duration REAL NOT NULL,
    video_codec TEXT,
    width INTEGER,
    height INTEGER,
    visual_type TEXT NOT NULL DEFAULT 'unknown',
    visual_confidence REAL,
    probe_json TEXT NOT NULL,
    overall_status TEXT NOT NULL DEFAULT 'new',
    last_error TEXT,
    last_seen TEXT NOT NULL,
    manager_output_fingerprint TEXT,
    present INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stages (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    tool_version TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(media_id, stage)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    pid INTEGER,
    hostname TEXT,
    command TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE TABLE IF NOT EXISTS overrides (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(media_id, key)
);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(overall_status);
CREATE INDEX IF NOT EXISTS idx_stages_status ON stages(status);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(media)").fetchall()}
        if "present" not in columns:
            self.conn.execute("ALTER TABLE media ADD COLUMN present INTEGER NOT NULL DEFAULT 1")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get_by_rel_path(self, rel_path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM media WHERE rel_path=?", (rel_path,)).fetchone()

    def upsert_media(
        self,
        *,
        rel_path: str,
        identity: MediaIdentity,
        stat_result,
        fingerprint: str,
        probe: ProbeInfo,
        external_change: bool,
    ) -> tuple[int, bool, bool]:
        now = now_iso()
        row = self.get_by_rel_path(rel_path)
        probe_json = json.dumps(probe.raw, ensure_ascii=False)
        if row is None:
            moved = self.conn.execute(
                "SELECT * FROM media WHERE present=0 AND fingerprint=? ORDER BY id", (fingerprint,)
            ).fetchall()
            if len(moved) == 1:
                old = moved[0]
                self.conn.execute(
                    """UPDATE media SET rel_path=?,media_type=?,title=?,year=?,show_title=?,season=?,episode=?,episode_title=?,
                       size=?,mtime_ns=?,duration=?,video_codec=?,width=?,height=?,probe_json=?,present=1,last_seen=?,last_error=NULL
                       WHERE id=?""",
                    (rel_path, identity.media_type.value, identity.title, identity.year, identity.show_title, identity.season,
                     identity.episode, identity.episode_title, stat_result.st_size, stat_result.st_mtime_ns, probe.duration,
                     probe.video_codec, probe.width, probe.height, probe_json, now, old["id"]),
                )
                self.add_event(int(old["id"]), "move", "Media path changed; existing fingerprint/history reconnected",
                               {"from": old["rel_path"], "to": rel_path})
                self.conn.commit()
                return int(old["id"]), False, False
            cur = self.conn.execute(
                """INSERT INTO media(
                    rel_path,media_type,title,year,show_title,season,episode,episode_title,
                    size,mtime_ns,fingerprint,duration,video_codec,width,height,probe_json,
                    overall_status,last_seen
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rel_path, identity.media_type.value, identity.title, identity.year,
                    identity.show_title, identity.season, identity.episode, identity.episode_title,
                    stat_result.st_size, stat_result.st_mtime_ns, fingerprint, probe.duration,
                    probe.video_codec, probe.width, probe.height, probe_json,
                    "new", now,
                ),
            )
            media_id = int(cur.lastrowid)
            self.conn.commit()
            return media_id, True, True

        changed = row["fingerprint"] != fingerprint
        self.conn.execute(
            """UPDATE media SET media_type=?,title=?,year=?,show_title=?,season=?,episode=?,episode_title=?,
                size=?,mtime_ns=?,fingerprint=?,duration=?,video_codec=?,width=?,height=?,probe_json=?,last_seen=?,present=1
                WHERE id=?""",
            (
                identity.media_type.value, identity.title, identity.year, identity.show_title,
                identity.season, identity.episode, identity.episode_title,
                stat_result.st_size, stat_result.st_mtime_ns, fingerprint, probe.duration,
                probe.video_codec, probe.width, probe.height, probe_json, now, row["id"],
            ),
        )
        if changed and external_change:
            self.conn.execute("DELETE FROM stages WHERE media_id=?", (row["id"],))
            self.conn.execute(
                "UPDATE media SET overall_status='changed', last_error=NULL, visual_type='unknown', visual_confidence=NULL WHERE id=?",
                (row["id"],),
            )
            self.add_event(int(row["id"]), "external_change", "Media fingerprint changed; pipeline state invalidated")
        self.conn.commit()
        return int(row["id"]), changed, False

    def update_after_manager_write(
        self,
        media_id: int,
        *,
        stat_result,
        fingerprint: str,
        probe: ProbeInfo,
    ) -> None:
        self.conn.execute(
            """UPDATE media SET size=?,mtime_ns=?,fingerprint=?,manager_output_fingerprint=?,duration=?,
                video_codec=?,width=?,height=?,probe_json=?,last_seen=?,present=1 WHERE id=?""",
            (
                stat_result.st_size, stat_result.st_mtime_ns, fingerprint, fingerprint,
                probe.duration, probe.video_codec, probe.width, probe.height,
                json.dumps(probe.raw, ensure_ascii=False), now_iso(), media_id,
            ),
        )
        self.conn.commit()

    def mark_seen_only(self, media_id: int, stat_result) -> None:
        self.conn.execute(
            "UPDATE media SET size=?,mtime_ns=?,last_seen=?,present=1 WHERE id=?",
            (stat_result.st_size, stat_result.st_mtime_ns, now_iso(), media_id),
        )
        self.conn.commit()

    def begin_scan(self) -> None:
        self.conn.execute("UPDATE media SET present=0")
        self.conn.commit()

    def mark_present_rel(self, rel_path: str) -> None:
        self.conn.execute("UPDATE media SET present=1,last_seen=? WHERE rel_path=?", (now_iso(), rel_path))
        self.conn.commit()

    def set_stage(
        self,
        media_id: int,
        stage: str,
        status: StageStatus | str,
        details: dict[str, Any] | None = None,
        tool_version: str | None = None,
    ) -> None:
        status_value = status.value if isinstance(status, StageStatus) else status
        self.conn.execute(
            """INSERT INTO stages(media_id,stage,status,details_json,tool_version,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(media_id,stage) DO UPDATE SET status=excluded.status,
               details_json=excluded.details_json,tool_version=excluded.tool_version,updated_at=excluded.updated_at""",
            (media_id, stage, status_value, json.dumps(details or {}, ensure_ascii=False), tool_version, now_iso()),
        )
        self.conn.commit()

    def get_stage(self, media_id: int, stage: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM stages WHERE media_id=? AND stage=?", (media_id, stage)
        ).fetchone()

    def stages(self, media_id: int) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM stages WHERE media_id=?", (media_id,)).fetchall()
        return {row["stage"]: row for row in rows}

    def set_overall(self, media_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE media SET overall_status=?,last_error=? WHERE id=?", (status, error, media_id)
        )
        self.conn.commit()

    def set_visual_type(self, media_id: int, visual: VisualType, confidence: float | None) -> None:
        self.conn.execute(
            "UPDATE media SET visual_type=?,visual_confidence=? WHERE id=?",
            (visual.value, confidence, media_id),
        )
        self.conn.commit()

    def add_event(self, media_id: int | None, kind: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(media_id,created_at,kind,message,details_json) VALUES(?,?,?,?,?)",
            (media_id, now_iso(), kind, message, json.dumps(details or {}, ensure_ascii=False)),
        )
        self.conn.commit()

    def rows(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM media WHERE {where} ORDER BY rel_path", tuple(params)).fetchall()

    def get_media(self, media_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None:
            raise KeyError(media_id)
        return row

    def find_media_by_path(self, rel_or_abs: str, root: Path) -> sqlite3.Row | None:
        p = Path(rel_or_abs)
        if p.is_absolute():
            try:
                rel = str(p.resolve().relative_to(root.resolve()))
            except ValueError:
                return None
        else:
            rel = str(p)
        return self.get_by_rel_path(rel)




    def recover_interrupted_state(self) -> dict[str, int]:
        """Make abandoned RUNNING records safely resumable.

        Checkpoint files are left untouched; only volatile database state is
        converted back to pending/incomplete.
        """
        running_stages = self.conn.execute(
            "SELECT media_id,stage FROM stages WHERE status='running'"
        ).fetchall()
        media_ids = sorted({int(r["media_id"]) for r in running_stages})
        self.conn.execute(
            "UPDATE stages SET status='pending',updated_at=? WHERE status='running'",
            (now_iso(),),
        )
        for media_id in media_ids:
            self.conn.execute(
                "UPDATE media SET overall_status='incomplete',last_error=NULL WHERE id=?",
                (media_id,),
            )
        run_cur = self.conn.execute(
            "UPDATE runs SET status='interrupted',ended_at=?,reason=COALESCE(reason,'Previous process ended without closing the run') WHERE status='running'",
            (now_iso(),),
        )
        self.conn.commit()
        return {"stages": len(running_stages), "runs": int(run_cur.rowcount)}

    def start_run(self, *, pid: int, hostname: str, command: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at,status,pid,hostname,command) VALUES(?,?,?,?,?)",
            (now_iso(), "running", int(pid), hostname, command),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?,ended_at=?,reason=? WHERE id=?",
            (status, now_iso(), reason, int(run_id)),
        )
        self.conn.commit()

    def active_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def reset_stages(self, statuses: set[str]) -> int:
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        rows = self.conn.execute(
            f"SELECT DISTINCT media_id FROM stages WHERE status IN ({placeholders})", tuple(statuses)
        ).fetchall()
        media_ids = [int(r["media_id"]) for r in rows]
        cur = self.conn.execute(
            f"DELETE FROM stages WHERE status IN ({placeholders})", tuple(statuses)
        )
        for media_id in media_ids:
            self.conn.execute(
                "UPDATE media SET overall_status='incomplete',last_error=NULL WHERE id=?", (media_id,)
            )
        self.conn.commit()
        return cur.rowcount

    def set_override(self, media_id: int, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO overrides(media_id,key,value) VALUES(?,?,?)
               ON CONFLICT(media_id,key) DO UPDATE SET value=excluded.value""",
            (media_id, key, value),
        )
        self.conn.commit()

    def get_override(self, media_id: int, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM overrides WHERE media_id=? AND key=?", (media_id, key)
        ).fetchone()
        return str(row["value"]) if row else None

    def stage_counts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT stage,status,COUNT(*) AS n FROM stages GROUP BY stage,status ORDER BY stage,status"
        ).fetchall()
