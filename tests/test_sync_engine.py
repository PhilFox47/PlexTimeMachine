"""Tests der Kernlogik: Suche, Sortierung, Blacklist, Playlist-Pflege."""

from __future__ import annotations

from datetime import date

import pytest

from app import db
from app.sync_engine import (
    apply_blacklist,
    apply_playlist,
    build_preview,
    collect_items,
    sync_all_users,
    sync_user,
    to_preview_item,
)
from tests.conftest import FakeEpisode, FakeGateway, FakeMovie, FakeServer

ERA_START = date(1985, 1, 1)
ERA_END = date(1985, 12, 31)


def test_collect_merges_and_sorts_chronologically(gateway):
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END)

    assert [i.display_title for i in items] == [
        "Brazil",
        "Das A-Team – S03E05 Showdown",
        "Zurück in die Zukunft",
        "Knight Rider – S01E01 Pilot",
        "Knight Rider – S01E02 Folge 2",
    ]
    # Gleiches Datum: Film vor Episode, Episoden in Reihenfolge der Nummer.
    assert items[0].air_date == items[1].air_date == date(1985, 2, 22)


def test_collect_filters_out_of_range_items(gateway):
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END)
    assert all(ERA_START <= i.air_date <= ERA_END for i in items)
    assert "Matrix" not in [i.title for i in items]


def test_date_filters_keep_leading_zeros_in_the_year(gateway):
    """Plex weist "200-02-06" ab und die Suche fällt auf einen Vollscan zurück.

    strftime("%Y") lässt bei Jahren unter 1000 die führenden Nullen weg –
    genau daran ist der Filter in einer echten Installation gescheitert.
    """
    from app.sync_engine import _date_filters

    filters = _date_filters(date(200, 2, 7), date(200, 2, 14))

    assert filters["originallyAvailableAt>>"] == "0200-02-06"
    assert filters["originallyAvailableAt<<"] == "0200-02-15"

    modern = _date_filters(date(1985, 1, 1), date(1985, 12, 31))
    assert modern["originallyAvailableAt>>"] == "1984-12-31"


def test_range_boundaries_are_inclusive(gateway):
    items = collect_items(gateway, gateway.server, date(1985, 2, 22), date(1985, 2, 22))
    assert {i.display_title for i in items} == {
        "Brazil",
        "Das A-Team – S03E05 Showdown",
    }


def test_search_falls_back_when_server_filters_unsupported(plex_data):
    server = FakeServer(plex_data["movies"], plex_data["episodes"], fail_filters=True)
    gw = FakeGateway(server)

    items = collect_items(gw, server, ERA_START, ERA_END)

    assert len(items) == 5  # Fallback filtert clientseitig
    assert server.movie_section.calls[-1]["kwargs"] == {"unwatched": True}


def test_search_falls_back_to_an_unfiltered_scan(plex_data, monkeypatch):
    """Selbst wenn Plex keinen Filter annimmt, bleibt das Ergebnis richtig."""
    server = FakeServer(plex_data["movies"], plex_data["episodes"])

    def stur(self, title=None, libtype=None, filters=None, maxresults=None, **kwargs):
        if filters or kwargs:
            raise RuntimeError("Unknown filter field")
        return [i for i in self._items if libtype is None or i.type == libtype]

    monkeypatch.setattr(type(server.movie_section), "search", stur)
    monkeypatch.setattr(type(server.tv_section), "search", stur)
    plex_data["movies"][1].viewCount = 1  # Brazil gesehen
    gw = FakeGateway(server)

    items = collect_items(gw, server, ERA_START, ERA_END)

    assert "Brazil" not in [i.title for i in items]      # trotzdem aussortiert
    assert len(items) == 4


def test_watched_items_never_enter_the_playlist(gateway, plex_data):
    """Der serverseitige Filter wird nicht blind geglaubt."""
    plex_data["movies"][0].viewCount = 1        # Zurück in die Zukunft gesehen
    plex_data["episodes"][0].viewCount = 3      # Knight-Rider-Pilot gesehen

    items = collect_items(gateway, gateway.server, ERA_START, ERA_END)

    assert [i.display_title for i in items] == [
        "Brazil",
        "Das A-Team – S03E05 Showdown",
        "Knight Rider – S01E02 Folge 2",
    ]


def test_episode_blacklist_uses_series_key(gateway):
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END)

    kept, dropped = apply_blacklist(items, {"100"})

    assert dropped == 2  # beide Knight-Rider-Episoden
    assert all(i.series_title != "Knight Rider" for i in kept)


def test_movie_blacklist_uses_rating_key(gateway):
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END)

    kept, dropped = apply_blacklist(items, {"1"})

    assert dropped == 1
    assert "Zurück in die Zukunft" not in [i.title for i in kept]


def test_to_preview_item_ignores_unsupported_types():
    class Artist:
        type = "artist"
        ratingKey = 5

    assert to_preview_item(Artist()) is None


def test_preview_reports_counts_and_respects_blacklist(session, gateway):
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")

    preview = build_preview(session, "Alex", ERA_START, ERA_END, gateway=gateway)

    assert preview.ok
    assert preview.total == 3
    assert preview.blacklisted == 2
    assert preview.movies == 2
    assert preview.episodes == 1
    assert not preview.truncated


def test_preview_truncates_to_limit(session, gateway):
    preview = build_preview(session, "Alex", ERA_START, ERA_END, gateway=gateway, limit=2)

    assert preview.truncated
    assert len(preview.items) == 2
    assert preview.total == 5


def test_preview_reports_plex_errors(session):
    from app.plex_client import PlexUnavailable

    class BrokenGateway:
        def connect_as(self, user_id):
            raise PlexUnavailable("Server offline")

    preview = build_preview(session, "Alex", ERA_START, ERA_END, gateway=BrokenGateway())

    assert not preview.ok
    assert "offline" in preview.error


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


def test_apply_playlist_creates_when_missing(gateway):
    items = [i.plex_object for i in collect_items(gateway, gateway.server, ERA_START, ERA_END)]

    assert apply_playlist(gateway.server, "PTM – Alex", items).exists is True
    playlist = gateway.server.playlists()[0]
    assert playlist.title == "PTM – Alex"
    assert playlist.items() == items


def test_apply_playlist_clears_and_refills_same_playlist(gateway):
    old = [FakeMovie(99, "Alt", "1970-01-01")]
    gateway.server.createPlaylist("PTM – Alex", items=old)
    new = [i.plex_object for i in collect_items(gateway, gateway.server, ERA_START, ERA_END)]

    apply_playlist(gateway.server, "PTM – Alex", new)

    assert len(gateway.server.playlists()) == 1
    playlist = gateway.server.playlists()[0]
    assert playlist.items() == new  # geleert und in neuer Reihenfolge befüllt
    assert gateway.server.created == ["PTM – Alex"]  # keine zweite Playlist


def test_unchanged_playlist_is_left_alone(gateway):
    """Beim Polling ändert sich meist nichts – dann darf nichts geschrieben werden.

    plexapi löscht Einträge einzeln: eine 800er-Playlist kostet sonst bei
    jedem Lauf 800 Anfragen an den Plex-Server.
    """
    items = [i.plex_object for i in collect_items(gateway, gateway.server, ERA_START, ERA_END)]
    apply_playlist(gateway.server, "PTM – Alex", items)
    playlist = gateway.server.playlists()[0]
    playlist.schreibzugriffe = 0

    ergebnis = apply_playlist(gateway.server, "PTM – Alex", items)

    assert ergebnis.unchanged is True and ergebnis.exists
    assert playlist.schreibzugriffe == 0
    assert playlist.items() == items


def test_changed_playlist_is_rewritten(gateway):
    items = [i.plex_object for i in collect_items(gateway, gateway.server, ERA_START, ERA_END)]
    apply_playlist(gateway.server, "PTM – Alex", items)
    playlist = gateway.server.playlists()[0]
    playlist.schreibzugriffe = 0

    ergebnis = apply_playlist(gateway.server, "PTM – Alex", items[:2])

    assert ergebnis.unchanged is False
    assert playlist.schreibzugriffe > 0
    assert gateway.server.playlists()[0].items() == items[:2]


def test_apply_playlist_deletes_playlist_when_no_matches(gateway):
    gateway.server.createPlaylist("PTM – Alex", items=[FakeMovie(99, "Alt", "1970-01-01")])

    assert apply_playlist(gateway.server, "PTM – Alex", []).exists is False
    assert gateway.server.playlists() == []


def test_apply_playlist_adds_in_chunks(monkeypatch, gateway):
    monkeypatch.setattr("app.sync_engine.PLAYLIST_CHUNK_SIZE", 2)
    items = [FakeMovie(i, f"Film {i}", "1985-05-05") for i in range(5)]

    apply_playlist(gateway.server, "PTM – Alex", items)

    assert gateway.server.playlists()[0].items() == items


def test_apply_playlist_survives_plex_dropping_empty_playlist(gateway):
    """Plex löscht eine leer geräumte Playlist mitunter selbst."""

    class SelfDeletingPlaylist:
        title = "PTM – Alex"

        def __init__(self, server):
            self.server = server

        def items(self):
            return [FakeMovie(99, "Alt", "1970-01-01")]

        def removeItems(self, items):
            self.server._playlists.remove(self)

    gateway.server._playlists.append(SelfDeletingPlaylist(gateway.server))
    items = [FakeMovie(1, "Neu", "1985-05-05")]

    assert apply_playlist(gateway.server, "PTM – Alex", items).exists is True
    assert gateway.server.playlists()[0].items() == items


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def test_sync_user_writes_state_and_logbook(session, gateway):
    db.set_period(session, "Alex", ERA_START, ERA_END)

    result = sync_user(session, "Alex", trigger="manual", gateway=gateway)

    assert result.ok
    assert result.item_count == 5
    assert result.playlist_name == "Plex Time Machine – Alex"

    state = db.get_or_create_user_state(session, "Alex")
    assert state.last_item_count == 5
    assert state.last_synced_at is not None

    journeys = db.list_journeys(session, "Alex")
    assert len(journeys) == 1 and journeys[0].item_count == 5
    assert journeys[0].trigger == "manual"


def test_sync_user_requires_period(session, gateway):
    result = sync_user(session, "Alex", gateway=gateway)

    assert not result.ok
    assert "Zeitraum" in result.error
    assert db.list_journeys(session, "Alex") == []


def test_sync_user_connects_in_user_context(session, gateway):
    db.set_period(session, "Nina", ERA_START, ERA_END)

    sync_user(session, "Nina", gateway=gateway)

    assert gateway.connections == ["Nina"]


def test_sync_user_notes_blacklisted_count(session, gateway):
    db.set_period(session, "Alex", ERA_START, ERA_END)
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")

    result = sync_user(session, "Alex", gateway=gateway)

    assert result.item_count == 3
    assert "2 durch Blacklist" in db.list_journeys(session, "Alex")[0].note


def test_sync_all_users_skips_users_without_period(session, gateway):
    db.set_period(session, "Alex", ERA_START, ERA_END)
    db.get_or_create_user_state(session, "Nina")  # kein Zeitraum

    results = sync_all_users(session, trigger="poll", gateway=gateway)

    assert [r.user_id for r in results] == ["Alex"]
    assert results[0].trigger == "poll"


def test_sync_reversed_period_is_normalised(session, gateway):
    db.set_period(session, "Alex", ERA_END, ERA_START)
    state = db.get_or_create_user_state(session, "Alex")

    assert state.current_date_start == ERA_START
    assert state.current_date_end == ERA_END
