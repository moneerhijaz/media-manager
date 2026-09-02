from __future__ import annotations

import re
from pathlib import Path

from .config import LibraryConfig
from .models import MediaIdentity, MediaType

YEAR_RE = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
EP_RE = re.compile(r"(?i)(?<![a-z0-9])s(?P<season>\d{1,3})e(?P<episode>\d{1,3})(?!\d)")
SEASON_DIR_RE = re.compile(r"(?i)^season\s+(\d{1,3})$")


def clean(text: str) -> str:
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s*-\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_year(text: str) -> tuple[str, int | None]:
    text = clean(Path(text).stem)
    matches = list(YEAR_RE.finditer(text))
    if matches:
        m = matches[-1]
        prefix = text[:m.start()].strip().rstrip("([{- ").strip()
        if prefix:
            return prefix, int(m.group(1))
    return text.strip(), None


def identify(path: Path, cfg: LibraryConfig) -> MediaIdentity | None:
    rp = path.resolve()
    try:
        rel_movies = rp.relative_to(cfg.movies_root.resolve())
    except (ValueError, FileNotFoundError):
        rel_movies = None
    try:
        rel_tv = rp.relative_to(cfg.tv_root.resolve())
    except (ValueError, FileNotFoundError):
        rel_tv = None

    if rel_movies is not None:
        # First directory under any optional quality hierarchy that looks like a movie folder.
        folder = path.parent.name
        ftitle, fyear = title_year(folder)
        ptitle, pyear = title_year(path.stem)
        title = ftitle if fyear or ftitle.casefold() == ptitle.casefold() else ptitle
        year = fyear or pyear
        conflict = None
        if EP_RE.search(path.stem):
            conflict = "File is under Movies but filename contains an sXXeXX episode code"
        return MediaIdentity(MediaType.MOVIE, title=title, year=year, conflict=conflict)

    if rel_tv is not None:
        epm = EP_RE.search(path.stem)
        if not epm:
            return MediaIdentity(
                MediaType.EPISODE, title=path.stem,
                conflict="File is under TV Shows but filename lacks an sXXeXX episode code",
            )
        season = int(epm.group("season"))
        episode = int(epm.group("episode"))
        # Find Season XX ancestor and show directory immediately above it.
        season_dir = None
        show_dir = None
        for parent in path.parents:
            m = SEASON_DIR_RE.match(parent.name)
            if m:
                season_dir = parent
                show_dir = parent.parent
                folder_season = int(m.group(1))
                break
            if parent == cfg.tv_root:
                break
        conflict = None
        if season_dir is not None and folder_season != season:
            conflict = f"Folder says Season {folder_season:02d}, filename says s{season:02d}e{episode:02d}"
        if show_dir is None:
            show_dir = path.parent.parent if path.parent.parent != path.parent else path.parent
        show_title, year = title_year(show_dir.name)
        normalized = clean(path.stem)
        em = EP_RE.search(normalized)
        episode_title = normalized[em.end():].strip(" -_") if em else None
        return MediaIdentity(
            MediaType.EPISODE,
            title=show_title,
            year=year,
            show_title=show_title,
            season=season,
            episode=episode,
            episode_title=episode_title or None,
            conflict=conflict,
        )

    return None
