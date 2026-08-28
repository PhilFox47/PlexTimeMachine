"""Test-Fixtures inkl. eines vollständigen Plex-Doubles."""

from __future__ import annotations

import os
import struct
import zlib
from datetime import datetime
from pathlib import Path
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
    monkeypatch.setenv("PTM_COVER_DIR", str(tmp_path / "covers"))
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
        self.viewCount = 0

    def markUnplayed(self):
        self.viewCount = 0
        return self

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
        self.viewCount = 0

    def markUnplayed(self):
        self.viewCount = 0
        return self

    def __repr__(self) -> str:  # pragma: no cover - nur für Testausgabe
        return f"<Episode {self.grandparentTitle} {self.title}>"


class FakeShow:
    type = "show"

    def __init__(self, rating_key: int, title: str, year: int, episodes: list["FakeEpisode"]):
        self.ratingKey = rating_key
        self.title = title
        self.year = year
        self.thumb = f"/library/metadata/{rating_key}/thumb/1"
        self._episodes = episodes
        self.leafCount = len(episodes)

    @property
    def viewedLeafCount(self) -> int:
        return sum(1 for e in self._episodes if getattr(e, "viewCount", 0))

    def markUnplayed(self):
        """Plex setzt beim Zurücksetzen einer Serie alle Episoden mit zurück."""
        for episode in self._episodes:
            episode.markUnplayed()
        return self

    def episodes(self) -> list["FakeEpisode"]:
        return list(self._episodes)

    def unwatched(self) -> list["FakeEpisode"]:
        return [e for e in self._episodes if not getattr(e, "viewCount", 0)]

    def __repr__(self) -> str:  # pragma: no cover - nur für Testausgabe
        return f"<Show {self.title}>"


class FakeSection:
    def __init__(
        self,
        title: str,
        type_: str,
        items: list[Any],
        fail_filters: bool = False,
        shows: list[FakeShow] | None = None,
    ):
        self.title = title
        self.type = type_
        self._items = items
        self.shows = shows or []
        self.fail_filters = fail_filters
        self.calls: list[dict] = []

    def search(
        self,
        title: str | None = None,
        libtype: str | None = None,
        filters: dict | None = None,
        maxresults: int | None = None,
        **kwargs,
    ):
        self.calls.append(
            {"title": title, "libtype": libtype, "filters": filters, "kwargs": kwargs}
        )
        if filters and self.fail_filters:
            raise RuntimeError("Unsupported filter field")
        found = [i for i in self._searchable() if libtype is None or i.type == libtype]
        if title:  # Plex sucht standardmäßig "enthält"
            found = [i for i in found if title.lower() in i.title.lower()]
        return found[:maxresults] if maxresults else found

    def _searchable(self):
        """Serien-Sektionen liefern je nach libtype Serien oder Episoden."""
        return list(self._items) + list(self.shows)


class FakePlaylist:
    def __init__(self, server: "FakeServer", title: str, items: list[Any]):
        self.server = server
        self.title = title
        self._items = list(items)
        self.deleted = False
        self.posters: list = []
        self.poster_deleted = False
        self.schreibzugriffe = 0

    def items(self) -> list[Any]:
        return list(self._items)

    def addItems(self, items) -> None:
        self._items.extend(items)
        self.schreibzugriffe += 1

    def removeItems(self, items) -> None:
        for item in items:
            self._items.remove(item)
            self.schreibzugriffe += 1  # plexapi löscht jeden Eintrag einzeln

    def editTitle(self, title: str, locked: bool = True) -> None:
        self.title = title

    def uploadPoster(self, url=None, filepath=None) -> None:
        self.posters.append(Path(filepath).read_bytes() if filepath else url)

    def deletePoster(self) -> None:
        self.posters.clear()
        self.poster_deleted = True

    def delete(self) -> None:
        self.deleted = True
        self.server._playlists = [p for p in self.server._playlists if p is not self]


class FakeClip:
    """Ein Video in der Übergangs-Bibliothek."""

    type = "movie"

    def __init__(self, rating_key: int, title: str):
        self.ratingKey = rating_key
        self.title = title

    def __repr__(self) -> str:  # pragma: no cover - nur für Testausgabe
        return f"<Clip {self.title}>"


class FakeTransitionSection:
    """Bibliothek vom Typ "Andere Videos": sieht Dateien erst nach dem Scan."""

    type = "movie"

    def __init__(self, ordner, title: str = "Zeitreise-Übergänge"):
        self.title = title
        self.ordner = ordner
        self.sichtbar: list = []
        self.scans = 0

    def update(self, **kwargs) -> None:
        from pathlib import Path

        self.scans += 1
        self.sichtbar = [
            FakeClip(9000 + i, p.stem)
            for i, p in enumerate(sorted(Path(self.ordner).glob("*.mp4")))
        ]

    def search(self, title=None, libtype=None, maxresults=None, **kwargs):
        treffer = [c for c in self.sichtbar if title is None or title.lower() in c.title.lower()]
        return treffer[:maxresults] if maxresults else treffer


class FakeLibrary:
    def __init__(self, abschnitte: dict):
        self.abschnitte = abschnitte

    def section(self, name):
        from plexapi.exceptions import NotFound

        if name not in self.abschnitte:
            raise NotFound(f"Unknown library section {name}")
        return self.abschnitte[name]


class FakeServer:
    def __init__(
        self,
        movies: list[Any],
        episodes: list[Any],
        fail_filters: bool = False,
        shows: list[FakeShow] | None = None,
    ):
        self.movie_section = FakeSection("Filme", "movie", movies, fail_filters)
        self.tv_section = FakeSection("Serien", "show", episodes, fail_filters, shows=shows)
        self._playlists: list[FakePlaylist] = []
        self.created: list[str] = []
        self.transition_section: FakeTransitionSection | None = None
        self.library = FakeLibrary({})

    def mit_uebergaengen(self, ordner, name: str = "Zeitreise-Übergänge"):
        """Bibliothek für die Übergangsclips anhängen."""
        self.transition_section = FakeTransitionSection(ordner, name)
        self.library = FakeLibrary({name: self.transition_section})
        return self

    def url(self, path, includeToken=False):
        return "http://plex.test" + path

    def fetchItem(self, rating_key: int):
        """Wie PlexServer.fetchItem: Zugriff über den ratingKey."""
        from plexapi.exceptions import NotFound

        pool = (
            list(self.movie_section._items)
            + list(self.tv_section._items)
            + list(self.tv_section.shows)
        )
        for item in pool:
            if item.ratingKey == rating_key:
                return item
        raise NotFound(f"Unknown ratingKey {rating_key}")

    def playlists(self) -> list[FakePlaylist]:
        return list(self._playlists)

    def createPlaylist(self, title: str, items=None) -> FakePlaylist:
        playlist = FakePlaylist(self, title, list(items or []))
        self._playlists.append(playlist)
        self.created.append(title)
        return playlist


class FakeGateway:
    """Duck-Type-Ersatz für PlexGateway."""

    def __init__(
        self,
        server: FakeServer,
        users: Optional[list] = None,
        servers: Optional[dict[str, FakeServer]] = None,
    ):
        from app.plex_client import HomeUser

        self.server = server
        # Je Nutzer ein eigener Server-Doppelgänger, um unterschiedliche
        # Watch-Stände abzubilden; ohne Eintrag gilt der Standard.
        self.servers = servers or {}
        self.connections: list[str] = []
        self.users = users if users is not None else [
            HomeUser(id="Alex", title="Alex", is_admin=True),
            HomeUser(id="Nina", title="Nina"),
        ]

    def home_users(self) -> list:
        return list(self.users)

    def connect_as(self, user_id: str) -> FakeServer:
        self.connections.append(user_id)
        return self.servers.get(user_id, self.server)

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
    shows = [
        FakeShow(100, "Knight Rider", 1982, [e for e in episodes if e.grandparentRatingKey == 100]),
        FakeShow(200, "Das A-Team", 1983, [e for e in episodes if e.grandparentRatingKey == 200]),
    ]
    return {"movies": movies, "episodes": episodes, "shows": shows}


@pytest.fixture
def client(gateway):
    """Die App über HTTP, mit dem Plex-Double dahinter."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.plex_client import set_gateway

    set_gateway(gateway)
    with TestClient(app) as test_client:
        yield test_client
    set_gateway(None)


@pytest.fixture
def gateway(plex_data) -> FakeGateway:
    return FakeGateway(
        FakeServer(plex_data["movies"], plex_data["episodes"], shows=plex_data["shows"])
    )


def make_png(width: int = 2, height: int = 3, filler: bytes = b"\xff\x60\x20") -> bytes:
    """Ein echtes (winziges) PNG – kein Attrappen-Byteschrott."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    raw = b"".join(b"\x00" + filler * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def png_image() -> bytes:
    return make_png()
