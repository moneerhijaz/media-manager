from __future__ import annotations

import hashlib
from pathlib import Path


BLOCK = 1024 * 1024


def quick_fingerprint(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(st.st_size).encode())
    with path.open("rb") as fh:
        h.update(fh.read(BLOCK))
        if st.st_size > BLOCK:
            fh.seek(max(0, st.st_size - BLOCK))
            h.update(fh.read(BLOCK))
    return h.hexdigest()
