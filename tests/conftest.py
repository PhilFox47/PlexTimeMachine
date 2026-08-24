"""Test-Fixtures inkl. eines vollständigen Plex-Doubles."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

import pytest

os.environ.setdefault("PTM_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PTM_PLEX_TOKEN", "test-token")
os.environ.setdefault("PTM_MOVIE_LIBRARY", "Filme")
os.environ.setdefault("PTM_TV_LIBRARY", "Serien")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()
    yield
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture
def session():
    from sqlmodel import Session

    from app.db import get_engine

    with Session(get_engine()) as s:
        yield s


# ---------------------------------------------------------------------------
# Plex-Double
# ---------------------------------------------------------------------------


def _dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


class FakeMovie:
    type = "movie"

    def __init__(self, rating_key: int, title: str, air: str, duration: int = 5_400_000):
        self.ratingKey = rating_key
        self.title = title
        self.originallyAvailableAt = _dt(air)
        self.year = self.originallyAvailableAt.year
        self.thumb = f"/library/metadata/{rating_key}/thumb/1"
        self.duration = duration

    def __repr__(self) -> str:  # pragma: no cover - nur für Testausgabe
        return f"<Movie {self.title}>"


class FakeEpisode:
    type = "episode"

    def __init__(
        self,
        rating_key: int,
        show_key: int,
        show_title: str,
        title: str,
        air: str,
        season: int = 1,
        episode: int = 1,
    ):
        self.ratingKey = rating_key
        self.grandparentRatingKey = show_key
        self.grandparentTitle = show_title
        self.grandparentThumb = f"/library/metadata/{show_key}/thumb/1"
        self.parentIndex = season
        self.index = episode
        self.title = title
        self.originallyAvailableAt = _dt(air)
        self.year = self.originallyAvailableAt.year
        self.duration = 2_700_000

    def __repr__(self) -> str:  # pragma: no cover - nur für Testausgabe
        return f"<Episode {self.grandparentTitle} {self.title}>"


class FakeSection:
    def __init__(self, title: str, type_: str, items: list[Any], fail_filters: bool = False):
        self.title = title
        self.type = type_
        self._items = items
        self.fail_filters = fail_filters
        self.calls: list[dict] = []

    def search(self, libtype: str | None = None, filters: dict | None = None, **kwargs):
        self.calls.append({"libtype": libtype, "filters": filters, "kwargs": kwargs})
        if filters and self.fail_filters:
            raise RuntimeError("Unsupported filter field")
        return [i for i in self._items if libtype is None or i.type == libtype]


class FakePlaylist:
    def __init__(self, server: "FakeServer", title: str, items: list[Any]):
        self.server = server
        self.title = title
        self._items = list(items)
        self.deleted = False

    def items(self) -> list[Any]:
        return list(self._items)

    def addItems(self, items) -> None:
        self._items.extend(items)

    def removeItems(self, items) -> None:
        for item in items:
            self._items.remove(item)

    def delete(self) -> None:
        self.deleted = True
        self.server._playlists = [p for p in self.server._playlists if p is not self]


class FakeServer:
    def __init__(self, movies: list[Any], episodes: list[Any], fail_filters: bool = False):
        self.movie_section = FakeSection("Filme", "movie", movies, fail_filters)
        self.tv_section = FakeSection("Serien", "show", episodes, fail_filters)
        self._playlists: list[FakePlaylist] = []
        self.created: list[str] = []

    def playlists(self) -> list[FakePlaylist]:
        return list(self._playlists)

    def createPlaylist(self, title: str, items=None) -> FakePlaylist:
        playlist = FakePlaylist(self, title, list(items or []))
        self._playlists.append(playlist)
        self.created.append(title)
        return playlist


class FakeGateway:
    """Duck-Type-Ersatz für PlexGateway."""

    def __init__(self, server: FakeServer, users: Optional[list] = None):
        from app.plex_client import HomeUser

        self.server = server
        self.connections: list[str] = []
        self.users = users if users is not None else [
            HomeUser(id="Alex", title="Alex", is_admin=True),
            HomeUser(id="Nina", title="Nina"),
        ]

    def home_users(self) -> list:
        return list(self.users)

    def connect_as(self, user_id: str) -> FakeServer:
        self.connections.append(user_id)
        return self.server

    def movie_section(self, server: FakeServer) -> FakeSection:
        return server.movie_section

    def tv_section(self, server: FakeServer) -> FakeSection:
        return server.tv_section


@pytest.fixture
def plex_data() -> dict[str, list[Any]]:
    movies = [
        FakeMovie(1, "Zurück in die Zukunft", "1985-07-03"),
        FakeMovie(2, "Brazil", "1985-02-22"),
        FakeMovie(3, "Matrix", "1999-03-31"),
        FakeMovie(4, "Magnolia", "2000-02-02"),
    ]
    episodes = [
        FakeEpisode(11, 100, "Knight Rider", "Pilot", "1985-09-20", 1, 1),
        FakeEpisode(12, 100, "Knight Rider", "Folge 2", "1985-09-20", 1, 2),
        FakeEpisode(13, 200, "Das A-Team", "Showdown", "1985-02-22", 3, 5),
    ]
    return {"movies": movies, "episodes": episodes}


@pytest.fixture
def gateway(plex_data) -> FakeGateway:
    return FakeGateway(FakeServer(plex_data["movies"], plex_data["episodes"]))
