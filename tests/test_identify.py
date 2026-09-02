from pathlib import Path

from media_manager.config import LibraryConfig
from media_manager.identify import identify
from media_manager.models import MediaType


def test_movie_identity_uses_library_tree(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    p = tmp_path / "Movies" / "HD" / "Example Film (2024)" / "Example Film (2024).mp4"
    p.parent.mkdir(parents=True)
    p.touch()
    ident = identify(p, cfg)
    assert ident is not None
    assert ident.media_type == MediaType.MOVIE
    assert ident.title == "Example Film"
    assert ident.year == 2024
    assert ident.conflict is None


def test_tv_identity_from_plex_tree_and_filename(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    p = (
        tmp_path
        / "TV Shows"
        / "HD"
        / "Example Series (2025)"
        / "Season 01"
        / "Example Series (2025) - s01e02 - Second Chapter.mp4"
    )
    p.parent.mkdir(parents=True)
    p.touch()
    ident = identify(p, cfg)
    assert ident is not None
    assert ident.media_type == MediaType.EPISODE
    assert ident.show_title == "Example Series"
    assert ident.year == 2025
    assert ident.season == 1
    assert ident.episode == 2
    assert ident.episode_title == "Second Chapter"
    assert ident.conflict is None


def test_tv_season_conflict_is_reviewable(tmp_path: Path):
    cfg = LibraryConfig.init(tmp_path)
    p = (
        tmp_path
        / "TV Shows"
        / "Example Series (2025)"
        / "Season 01"
        / "Example Series (2025) - S02E02 - Second Chapter.mp4"
    )
    p.parent.mkdir(parents=True)
    p.touch()
    ident = identify(p, cfg)
    assert ident is not None
    assert ident.conflict is not None
    assert "Season 01" in ident.conflict
    assert "s02e02" in ident.conflict
