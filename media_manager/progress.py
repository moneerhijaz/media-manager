from __future__ import annotations

import getpass
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class _State:
    file_index: int = 0
    file_total: int = 0
    file_name: str = ""
    stage_index: int = 0
    stage_total: int = 0
    stage_name: str = "idle"
    completed: float | None = None
    total: float | None = None
    message: str = ""
    stage_started: float = 0.0


class ProgressDisplay:
    """Single-line, continuously refreshed TTY progress display.

    The renderer intentionally has no third-party dependency. Long blocking
    operations still visibly animate because a tiny background renderer updates
    the spinner/elapsed time while worker code is inside ffmpeg/SyncNet/etc.
    """

    def __init__(self, *, enabled: bool | None = None, stream=None):
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else bool(enabled)
        self._state = _State(stage_started=time.monotonic())
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._suspended = threading.Event()
        self._render_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._spinner_i = 0
        self._last_line = ""
        if self.enabled:
            self._thread = threading.Thread(target=self._render_loop, name="media-manager-progress", daemon=True)
            self._thread.start()

    def scan(self, message: str = "Scanning library") -> None:
        self._set(stage_name="scan", stage_index=0, stage_total=0, completed=None, total=None, message=message)

    def start_file(self, index: int, total: int, rel_path: str | Path, *, stage_total: int) -> None:
        with self._lock:
            self._state.file_index = int(index)
            self._state.file_total = int(total)
            self._state.file_name = str(rel_path)
            self._state.stage_index = 0
            self._state.stage_total = int(stage_total)
            self._state.stage_name = "starting"
            self._state.completed = None
            self._state.total = None
            self._state.message = "Preparing"
            self._state.stage_started = time.monotonic()
        self._dirty.set()

    def stage(
        self,
        stage_name: str,
        *,
        stage_index: int,
        stage_total: int | None = None,
        completed: float | None = None,
        total: float | None = None,
        message: str = "",
    ) -> None:
        with self._lock:
            self._state.stage_name = stage_name
            self._state.stage_index = int(stage_index)
            if stage_total is not None:
                self._state.stage_total = int(stage_total)
            self._state.completed = completed
            self._state.total = total
            self._state.message = message
            self._state.stage_started = time.monotonic()
        self._dirty.set()

    def update(
        self,
        *,
        completed: float | None = None,
        total: float | None = None,
        message: str | None = None,
        stage_name: str | None = None,
    ) -> None:
        with self._lock:
            if stage_name is not None:
                self._state.stage_name = stage_name
            if completed is not None:
                self._state.completed = completed
            if total is not None:
                self._state.total = total
            if message is not None:
                self._state.message = message
        self._dirty.set()

    def finish_file(self, status: str, message: str = "") -> None:
        with self._lock:
            self._state.completed = 1
            self._state.total = 1
            self._state.message = message or status
            self._state.stage_name = status
        if self.enabled:
            self._render(force=True)
            self.stream.write("\n")
            self.stream.flush()
            self._last_line = ""
        else:
            label = f"[{self._state.file_index}/{self._state.file_total}] {self._state.file_name}"
            print(f"{label}: {status}" + (f" — {message}" if message else ""), file=self.stream)

    def note(self, text: str) -> None:
        if self.enabled:
            self._clear_line()
        print(text, file=self.stream)
        if self.enabled:
            self._dirty.set()

    def prompt_secret(self, prompt: str) -> str:
        """Prompt without letting the live renderer overwrite the question.

        OpenSubtitles authentication happens lazily, after the progress thread
        has started. Suspend redraws while getpass owns the terminal, then
        resume the current stage display after input has been collected.
        """
        if not self.enabled:
            return getpass.getpass(prompt)
        self._suspended.set()
        try:
            with self._render_lock:
                self._clear_line_unlocked()
                return getpass.getpass(prompt, stream=self.stream)
        finally:
            self._suspended.clear()
            self._dirty.set()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self.enabled and self._last_line:
            self._clear_line()

    def _set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self._state, k, v)
            self._state.stage_started = time.monotonic()
        self._dirty.set()

    def _render_loop(self) -> None:
        while not self._stop.is_set():
            if not self._suspended.is_set():
                self._render()
            self._stop.wait(0.15)

    def _bar(self, completed: float | None, total: float | None, width: int = 22) -> tuple[str, str]:
        if completed is None or total is None or total <= 0:
            spinner = _SPINNER[self._spinner_i % len(_SPINNER)]
            self._spinner_i += 1
            return f"[{spinner}{' ' * (width - 1)}]", ""
        frac = max(0.0, min(1.0, float(completed) / float(total)))
        filled = int(round(width * frac))
        bar = "█" * filled + "░" * (width - filled)
        pct = f"{frac * 100:5.1f}%"
        return f"[{bar}]", pct

    def _render(self, *, force: bool = False) -> None:
        if not self.enabled or self._suspended.is_set():
            return
        with self._lock:
            s = _State(**self._state.__dict__)
        bar, pct = self._bar(s.completed, s.total)
        elapsed = max(0, int(time.monotonic() - s.stage_started))
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        elapsed_text = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"

        file_bits = ""
        if s.file_total:
            name = Path(s.file_name).name
            file_bits = f"[{s.file_index}/{s.file_total}] {name}  "
        stage_bits = s.stage_name.upper()
        if s.stage_total and s.stage_index:
            stage_bits += f" {s.stage_index}/{s.stage_total}"
        count_bits = ""
        if s.completed is not None and s.total is not None and s.total > 1:
            count_bits = f" {int(s.completed)}/{int(s.total)}"
        message = f"  {s.message}" if s.message else ""
        line = f"{file_bits}{stage_bits} {bar} {pct}{count_bits}  {elapsed_text}{message}".rstrip()
        width = max(40, shutil.get_terminal_size((120, 20)).columns)
        if len(line) >= width:
            line = line[: width - 1]
        with self._render_lock:
            if self._suspended.is_set():
                return
            if force or line != self._last_line:
                self.stream.write("\r\x1b[2K" + line)
                self.stream.flush()
                self._last_line = line

    def _clear_line(self) -> None:
        with self._render_lock:
            self._clear_line_unlocked()

    def _clear_line_unlocked(self) -> None:
        if self.enabled:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()
            self._last_line = ""
