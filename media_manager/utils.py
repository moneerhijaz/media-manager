from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


class MediaManagerError(RuntimeError):
    pass


class ExternalToolError(MediaManagerError):
    pass


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaManagerError(f"Required executable not found in PATH: {name}")
    return path


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalToolError(
            f"Command timed out after {float(timeout):g} seconds: {' '.join(cmd)}"
        ) from exc
    if check and proc.returncode != 0:
        stderr = proc.stderr if isinstance(proc.stderr, str) else ""
        raise ExternalToolError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr[-4000:]}"
        )
    return proc


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def human_size(num: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return str(num)


def atomic_replace(src: Path, dst: Path, source_mode: int | None = None) -> None:
    if source_mode is not None:
        os.chmod(src, source_mode)
    os.replace(src, dst)


def relative_or_abs(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def chunked(items: Iterable, n: int):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch
