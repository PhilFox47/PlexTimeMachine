"""Tests für Suche, Zusammenstellung und Playlist-Bau des Almanachs."""

from __future__ import annotations

from datetime import date

import pytest

from app import db
from app.almanach import (
    build_preview,
    collect_almanach_items,
    search_titles,
    sync_all_almanachs,
    sync_almanach,
)

# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def test_search_finds_shows_and_movies_by_partial_title(session, gateway):
    result = search_titles(session, "Alex", "rider", gateway=gateway)

    assert result.ok
    assert [(h.title, h.media_type) for h in result.hits] == [("Knight Rider", "show")]
    assert result.hits[0].episode_count == 2  # Serie meldet ihren Umfang


def test_search_is_case_insensitive_and_matches_movies(session, gateway):
    result = search_titles(session, "Alex", "BRAZIL", gateway=gateway)

    assert [h.title for h in result.hits] == ["Brazil"]
    assert result.hits[0].media_type == "movie"
    assert result.hits[0].year == 1985


def test_search_marks_entries_already_in_almanach(session, gateway):
    db.add_to_almanach(session, "Alex", "100", "show", "Knight Rider")

    result = search_titles(session, "Alex", "rider", gateway=gateway)

    assert result.hits[0].already_added is True


def test_search_without_query_returns_nothing(session, gateway):
    assert search_titles(session, "Alex", "   ", gateway=gateway).hits == []


def test_search_reports_plex_errors(session):
    from app.plex_client import PlexUnavailable

    class BrokenGateway:
        def connect_as(self, user_id):
            raise PlexUnavailable("Server offline")

    result = search_titles(session, "Alex", "star", gateway=BrokenGateway())

    assert not result.ok and "offline" in result.error


def test_search_respects_limit(session, gateway):
    result = search_titles(session, "Alex", "", gateway=gateway, limit=1)

    assert result.hits == []  # leere Suche liefert grundsätzlich nichts


# ---------------------------------------------------------------------------
# Zusammenstellen
# ---------------------------------------------------------------------------


def _entries(session, *specs):
    for key, media_type, title in specs:
        db.add_to_almanach(session, "Alex", key, media_type, title)
    return db.list_almanach(session, "Alex")


def test_collect_expands_show_into_episodes_and_keeps_release_order(session, gateway):
    entries = _entries(
        session, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft")
    )

    items, missing = collect_almanach_items(gateway.server, entries)

    assert missing == []
    assert [i.display_title for i in items] == [
        "Zurück in die Zukunft",
        "Knight Rider – S01E01 Pilot",
        "Knight Rider – S01E02 Folge 2",
    ]
    dates = [i.air_date for i in items]
    assert dates == sorted(dates)  # streng nach Erscheinungsdatum


def test_collect_interleaves_movies_and_episodes_chronologically(session, gateway):
    entries = _entries(
        session,
        ("200", "show", "Das A-Team"),
        ("1", "movie", "Zurück in die Zukunft"),
        ("2", "movie", "Brazil"),
    )

    items, _ = collect_almanach_items(gateway.server, entries)

    assert [i.display_title for i in items] == [
        "Brazil",                              # 22.02.1985
        "Das A-Team – S03E05 Showdown",        # 22.02.1985, Film vor Episode
        "Zurück in die Zukunft",               # 03.07.1985
    ]


def test_collect_skips_watched_items(session, gateway, plex_data):
    plex_data["movies"][0].viewCount = 1          # Zurück in die Zukunft gesehen
    plex_data["episodes"][0].viewCount = 2        # Knight Rider Pilot gesehen
    entries = _entries(
        session, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft")
    )

    items, _ = collect_almanach_items(gateway.server, entries)

    assert [i.display_title for i in items] == ["Knight Rider – S01E02 Folge 2"]


def test_collect_reports_entries_missing_from_library(session, gateway):
    entries = _entries(session, ("999", "show", "Verschwundene Serie"))

    items, missing = collect_almanach_items(gateway.server, entries)

    assert items == []
    assert missing == ["Verschwundene Serie"]


def test_collect_deduplicates_titles(session, gateway):
    """Ein Film, der zweimal im Bestand landet, kommt trotzdem nur einmal rein."""
    entries = list(_entries(session, ("1", "movie", "Zurück in die Zukunft")))
    entries = entries * 2

    items, _ = collect_almanach_items(gateway.server, entries)

    assert len(items) == 1


def test_preview_counts_movies_and_episodes(session, gateway):
    _entries(session, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    preview = build_preview(session, "Alex", gateway=gateway)

    assert preview.ok
    assert (preview.total, preview.movies, preview.episodes) == (3, 1, 2)


def test_preview_is_empty_without_entries(session, gateway):
    preview = build_preview(session, "Alex", gateway=gateway)

    assert preview.ok and preview.total == 0


def test_preview_truncates_to_limit(session, gateway):
    _entries(session, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    preview = build_preview(session, "Alex", gateway=gateway, limit=2)

    assert preview.truncated and len(preview.items) == 2 and preview.total == 3


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


def test_sync_writes_own_playlist_and_logs_journey(session, gateway):
    _entries(session, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft"))

    result = sync_almanach(session, "Alex", trigger="manual", gateway=gateway)

    assert result.ok and result.item_count == 3
    assert result.playlist_name == "Plex Almanach – Alex"

    playlist = gateway.server.playlists()[0]
    assert playlist.title == "Plex Almanach – Alex"
    assert len(playlist.items()) == 3

    state = db.get_or_create_almanach_state(session, "Alex")
    assert state.last_item_count == 3 and state.last_synced_at is not None

    journey = db.list_journeys(session, "Alex")[0]
    assert journey.kind == "almanach" and journey.item_count == 3
    assert journey.date_start is None  # der Almanach kennt keinen Zeitraum


def test_sync_requires_entries(session, gateway):
    result = sync_almanach(session, "Alex", gateway=gateway)

    assert not result.ok and "leer" in result.error
    assert db.list_journeys(session, "Alex") == []


def test_sync_ignores_blacklist_because_selection_is_explicit(session, gateway):
    """Wer eine Serie bewusst in den Almanach legt, will sie auch sehen."""
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")
    _entries(session, ("100", "show", "Knight Rider"))

    result = sync_almanach(session, "Alex", gateway=gateway)

    assert result.item_count == 2


def test_sync_reports_missing_entries_in_message(session, gateway):
    _entries(session, ("2", "movie", "Brazil"), ("999", "movie", "Weg damit"))

    result = sync_almanach(session, "Alex", gateway=gateway)

    assert result.ok and result.item_count == 1
    assert "Weg damit" in result.message
    assert "1 Einträge nicht mehr" in db.list_journeys(session, "Alex")[0].note


def test_sync_uses_user_context(session, gateway):
    _entries(session, ("2", "movie", "Brazil"))
    db.remove_from_almanach(session, "Alex", "2")
    db.add_to_almanach(session, "Nina", "2", "movie", "Brazil")

    sync_almanach(session, "Nina", gateway=gateway)

    assert gateway.connections == ["Nina"]


def test_sync_all_almanachs_covers_only_users_with_entries(session, gateway):
    db.add_to_almanach(session, "Alex", "2", "movie", "Brazil")
    db.get_or_create_almanach_state(session, "Nina")  # kein Bestand

    results = sync_all_almanachs(session, trigger="poll", gateway=gateway)

    assert [r.user_id for r in results] == ["Alex"]
    assert results[0].trigger == "poll"


def test_scheduler_run_syncs_both_playlist_kinds(session, gateway):
    from app.plex_client import set_gateway
    from app.scheduler import run_sync_all

    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    db.add_to_almanach(session, "Alex", "100", "show", "Knight Rider")

    set_gateway(gateway)
    try:
        run_sync_all("poll")
    finally:
        set_gateway(None)

    titles = sorted(p.title for p in gateway.server.playlists())
    assert titles == ["Plex Almanach – Alex", "Plex Time Machine – Alex"]
    assert {j.kind for j in db.list_journeys(session, "Alex")} == {"timemachine", "almanach"}
