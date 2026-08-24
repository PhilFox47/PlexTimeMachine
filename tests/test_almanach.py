"""Tests für Suche, Zusammenstellung und Playlist-Bau des Almanachs."""

from __future__ import annotations

from datetime import date

import pytest

from app import db
from app.almanach import (
    build_preview,
    collect_almanach_items,
    copy_almanach,
    copy_almanach_to_users,
    plan_reset,
    reset_watch_state,
    search_titles,
    sync_all_almanachs,
    sync_almanach,
)


@pytest.fixture
def almanach(session):
    return db.create_almanach(session, "Alex", "Star Wars")

# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def test_search_finds_shows_and_movies_by_partial_title(session, gateway, almanach):
    result = search_titles(session, almanach, "rider", gateway=gateway)

    assert result.ok
    assert [(h.title, h.media_type) for h in result.hits] == [("Knight Rider", "show")]
    assert result.hits[0].episode_count == 2  # Serie meldet ihren Umfang


def test_search_is_case_insensitive_and_matches_movies(session, gateway, almanach):
    result = search_titles(session, almanach, "BRAZIL", gateway=gateway)

    assert [h.title for h in result.hits] == ["Brazil"]
    assert result.hits[0].media_type == "movie"
    assert result.hits[0].year == 1985


def test_search_marks_entries_already_in_almanach(session, gateway, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    result = search_titles(session, almanach, "rider", gateway=gateway)

    assert result.hits[0].already_added is True


def test_search_without_query_returns_nothing(session, gateway, almanach):
    assert search_titles(session, almanach, "   ", gateway=gateway).hits == []


def test_search_reports_plex_errors(session, almanach):
    from app.plex_client import PlexUnavailable

    class BrokenGateway:
        def connect_as(self, user_id):
            raise PlexUnavailable("Server offline")

    result = search_titles(session, almanach, "star", gateway=BrokenGateway())

    assert not result.ok and "offline" in result.error


def test_search_respects_limit(session, gateway, almanach):
    result = search_titles(session, almanach, "", gateway=gateway, limit=1)

    assert result.hits == []  # leere Suche liefert grundsätzlich nichts


# ---------------------------------------------------------------------------
# Zusammenstellen
# ---------------------------------------------------------------------------


def _entries(session, almanach, *specs):
    for key, media_type, title in specs:
        db.add_to_almanach(session, almanach, key, media_type, title)
    return db.list_almanach_entries(session, almanach.id)


def test_collect_expands_show_into_episodes_and_keeps_release_order(session, gateway, almanach):
    entries = _entries(session, almanach, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft")
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


def test_collect_interleaves_movies_and_episodes_chronologically(session, gateway, almanach):
    entries = _entries(session, almanach,
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


def test_collect_skips_watched_items(session, gateway, plex_data, almanach):
    plex_data["movies"][0].viewCount = 1          # Zurück in die Zukunft gesehen
    plex_data["episodes"][0].viewCount = 2        # Knight Rider Pilot gesehen
    entries = _entries(session, almanach, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft")
    )

    items, _ = collect_almanach_items(gateway.server, entries)

    assert [i.display_title for i in items] == ["Knight Rider – S01E02 Folge 2"]


def test_collect_reports_entries_missing_from_library(session, gateway, almanach):
    entries = _entries(session, almanach, ("999", "show", "Verschwundene Serie"))

    items, missing = collect_almanach_items(gateway.server, entries)

    assert items == []
    assert missing == ["Verschwundene Serie"]


def test_collect_deduplicates_titles(session, gateway, almanach):
    """Ein Film, der zweimal im Bestand landet, kommt trotzdem nur einmal rein."""
    entries = list(_entries(session, almanach, ("1", "movie", "Zurück in die Zukunft")))
    entries = entries * 2

    items, _ = collect_almanach_items(gateway.server, entries)

    assert len(items) == 1


def test_preview_counts_movies_and_episodes(session, gateway, almanach):
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    preview = build_preview(session, almanach, gateway=gateway)

    assert preview.ok
    assert (preview.total, preview.movies, preview.episodes) == (3, 1, 2)


def test_preview_is_empty_without_entries(session, almanach, gateway):
    preview = build_preview(session, almanach, gateway=gateway)

    assert preview.ok and preview.total == 0


def test_preview_truncates_to_limit(session, gateway, almanach):
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    preview = build_preview(session, almanach, gateway=gateway, limit=2)

    assert preview.truncated and len(preview.items) == 2 and preview.total == 3


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


def test_sync_writes_own_playlist_and_logs_journey(session, gateway, almanach):
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft"))

    result = sync_almanach(session, almanach, trigger="manual", gateway=gateway)

    assert result.ok and result.item_count == 3
    assert result.playlist_name == "Plex Almanach – Alex · Star Wars"

    playlist = gateway.server.playlists()[0]
    assert playlist.title == "Plex Almanach – Alex · Star Wars"
    assert len(playlist.items()) == 3

    session.refresh(almanach)
    assert almanach.last_item_count == 3 and almanach.last_synced_at is not None

    journey = db.list_journeys(session, "Alex")[0]
    assert journey.kind == "almanach" and journey.item_count == 3
    assert journey.date_start is None  # der Almanach kennt keinen Zeitraum


def test_sync_requires_entries(session, almanach, gateway):
    result = sync_almanach(session, almanach, gateway=gateway)

    assert not result.ok and "leer" in result.error
    assert db.list_journeys(session, "Alex") == []


def test_sync_ignores_blacklist_because_selection_is_explicit(session, gateway, almanach):
    """Wer eine Serie bewusst in den Almanach legt, will sie auch sehen."""
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    result = sync_almanach(session, almanach, gateway=gateway)

    assert result.item_count == 2


def test_sync_reports_missing_entries_in_message(session, gateway, almanach):
    _entries(session, almanach, ("2", "movie", "Brazil"), ("999", "movie", "Weg damit"))

    result = sync_almanach(session, almanach, gateway=gateway)

    assert result.ok and result.item_count == 1
    assert "Weg damit" in result.message
    assert "1 Einträge nicht mehr" in db.list_journeys(session, "Alex")[0].note


def test_sync_uses_the_owning_user_context(session, gateway):
    nina = db.create_almanach(session, "Nina", "Brazil-Abend")
    db.add_to_almanach(session, nina, "2", "movie", "Brazil")

    sync_almanach(session, nina, gateway=gateway)

    assert gateway.connections == ["Nina"]


def test_sync_all_almanachs_covers_every_filled_collection(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    zweiter = db.create_almanach(session, "Alex", "Achtziger")
    db.add_to_almanach(session, zweiter, "1", "movie", "Zurück in die Zukunft")
    db.create_almanach(session, "Alex", "Leer")  # ohne Bestand

    results = sync_all_almanachs(session, trigger="poll", gateway=gateway)

    assert sorted(r.playlist_name for r in results) == [
        "Plex Almanach – Alex · Achtziger",
        "Plex Almanach – Alex · Star Wars",
    ]
    assert all(r.trigger == "poll" for r in results)


def test_scheduler_run_syncs_both_playlist_kinds(session, gateway, almanach):
    from app.plex_client import set_gateway
    from app.scheduler import run_sync_all

    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    set_gateway(gateway)
    try:
        run_sync_all("poll")
    finally:
        set_gateway(None)

    titles = sorted(p.title for p in gateway.server.playlists())
    assert titles == ["Plex Almanach – Alex · Star Wars", "Plex Time Machine – Alex"]
    assert {j.kind for j in db.list_journeys(session, "Alex")} == {"timemachine", "almanach"}


# ---------------------------------------------------------------------------
# Mehrere benannte Almanachs
# ---------------------------------------------------------------------------


def test_collections_are_independent(session, gateway):
    star_wars = db.create_almanach(session, "Alex", "Star Wars")
    achtziger = db.create_almanach(session, "Alex", "Achtziger")

    db.add_to_almanach(session, star_wars, "100", "show", "Knight Rider")
    db.add_to_almanach(session, achtziger, "1", "movie", "Zurück in die Zukunft")

    assert db.almanach_keys(session, star_wars.id) == {"100"}
    assert db.almanach_keys(session, achtziger.id) == {"1"}
    assert star_wars.target_playlist_name != achtziger.target_playlist_name


def test_get_almanach_refuses_other_users(session):
    fremd = db.create_almanach(session, "Nina", "Ninas Sammlung")

    assert db.get_almanach(session, "Alex", fremd.id) is None
    assert db.get_almanach(session, "Nina", fremd.id) is not None


def test_rename_updates_the_generated_playlist_name(session, almanach):
    db.rename_almanach(session, almanach, "Star Wars komplett")

    assert almanach.name == "Star Wars komplett"
    assert almanach.target_playlist_name == "Plex Almanach – Alex · Star Wars komplett"


def test_rename_keeps_a_hand_picked_playlist_name(session, almanach):
    almanach.target_playlist_name = "Meine eigene Playlist"
    session.add(almanach)
    session.commit()

    db.rename_almanach(session, almanach, "Neuer Name")

    assert almanach.target_playlist_name == "Meine eigene Playlist"


def test_delete_removes_collection_and_entries(session, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    db.delete_almanach(session, almanach)

    assert db.list_almanachs(session, "Alex") == []
    assert db.list_almanach_entries(session, almanach.id) == []


def test_delete_playlist_removes_the_plex_playlist(session, almanach, gateway):
    from app.almanach import delete_playlist

    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    sync_almanach(session, almanach, gateway=gateway)
    assert gateway.server.playlists()

    assert delete_playlist(almanach, gateway=gateway) is True
    assert gateway.server.playlists() == []


# ---------------------------------------------------------------------------
# Watch-Status zurücksetzen
# ---------------------------------------------------------------------------


def test_plan_reset_counts_watched_items(session, gateway, plex_data, almanach):
    plex_data["episodes"][0].viewCount = 1        # eine Knight-Rider-Folge gesehen
    plex_data["movies"][1].viewCount = 1          # Brazil gesehen
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    plan = plan_reset(session, almanach, gateway=gateway)

    assert plan.ok
    assert (plan.watched_episodes, plan.watched_movies) == (1, 1)
    assert (plan.total_episodes, plan.total_movies) == (2, 1)
    assert plan.watched_total == 2
    assert not plan.nothing_to_do


def test_plan_reset_knows_when_nothing_is_watched(session, gateway, almanach):
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    plan = plan_reset(session, almanach, gateway=gateway)

    assert plan.nothing_to_do and plan.watched_total == 0


def test_plan_reset_requires_entries(session, gateway, almanach):
    plan = plan_reset(session, almanach, gateway=gateway)

    assert not plan.ok and "leer" in plan.error


def test_plan_reset_does_not_change_anything(session, gateway, plex_data, almanach):
    plex_data["episodes"][0].viewCount = 1
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    plan_reset(session, almanach, gateway=gateway)

    assert plex_data["episodes"][0].viewCount == 1  # unverändert


def test_reset_marks_everything_unplayed(session, gateway, plex_data, almanach):
    plex_data["episodes"][0].viewCount = 1
    plex_data["episodes"][1].viewCount = 3
    plex_data["movies"][1].viewCount = 1
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    result = reset_watch_state(session, almanach, gateway=gateway)

    assert result.ok
    assert (result.episodes, result.movies, result.total) == (2, 1, 3)
    assert [e.viewCount for e in plex_data["episodes"][:2]] == [0, 0]
    assert plex_data["movies"][1].viewCount == 0


def test_reset_only_touches_its_own_collection(session, gateway, plex_data, almanach):
    plex_data["movies"][0].viewCount = 1          # Zurück in die Zukunft, nicht im Bestand
    plex_data["movies"][1].viewCount = 1          # Brazil, im Bestand
    _entries(session, almanach, ("2", "movie", "Brazil"))

    reset_watch_state(session, almanach, gateway=gateway)

    assert plex_data["movies"][0].viewCount == 1  # bleibt gesehen
    assert plex_data["movies"][1].viewCount == 0


def test_reset_skips_missing_entries(session, gateway, almanach):
    _entries(session, almanach, ("999", "movie", "Weg damit"), ("2", "movie", "Brazil"))

    result = reset_watch_state(session, almanach, gateway=gateway)

    assert result.ok and result.missing == ["Weg damit"]


def test_reset_requires_entries(session, gateway, almanach):
    result = reset_watch_state(session, almanach, gateway=gateway)

    assert not result.ok and "leer" in result.error


def test_reset_reports_plex_errors(session, almanach):
    from app.plex_client import PlexUnavailable

    class BrokenGateway:
        def connect_as(self, user_id):
            raise PlexUnavailable("Server offline")

    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    result = reset_watch_state(session, almanach, gateway=BrokenGateway())

    assert not result.ok and "offline" in result.error


def test_reset_makes_items_return_to_the_playlist(session, gateway, plex_data, almanach):
    """Nach dem Reset gehört alles wieder in die Playlist."""
    plex_data["episodes"][0].viewCount = 1
    plex_data["episodes"][1].viewCount = 1
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    assert sync_almanach(session, almanach, gateway=gateway).item_count == 0

    reset_watch_state(session, almanach, gateway=gateway)

    assert sync_almanach(session, almanach, gateway=gateway).item_count == 2


# ---------------------------------------------------------------------------
# In andere Profile übernehmen
# ---------------------------------------------------------------------------


def _nina_gateway(plex_data, watched_episode: bool = False):
    """Zweiter Server-Doppelgänger mit eigenem Watch-Stand für Nina."""
    from tests.conftest import FakeEpisode, FakeGateway, FakeMovie, FakeServer, FakeShow

    movies = [FakeMovie(m.ratingKey, m.title, m.originallyAvailableAt.strftime("%Y-%m-%d"))
              for m in plex_data["movies"]]
    episodes = [
        FakeEpisode(e.ratingKey, e.grandparentRatingKey, e.grandparentTitle, e.title,
                    e.originallyAvailableAt.strftime("%Y-%m-%d"), e.parentIndex, e.index)
        for e in plex_data["episodes"]
    ]
    if watched_episode:
        episodes[0].viewCount = 1  # Nina hat den Knight-Rider-Piloten schon gesehen
    shows = [
        FakeShow(100, "Knight Rider", 1982, [e for e in episodes if e.grandparentRatingKey == 100]),
        FakeShow(200, "Das A-Team", 1983, [e for e in episodes if e.grandparentRatingKey == 200]),
    ]
    ninas_server = FakeServer(movies, episodes, shows=shows)
    alex_server = FakeServer(plex_data["movies"], plex_data["episodes"], shows=plex_data["shows"])
    return FakeGateway(alex_server, servers={"Nina": ninas_server}), ninas_server


def test_copy_creates_the_collection_for_the_other_profile(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    result = copy_almanach(session, almanach, "Nina", gateway=gateway)

    assert result.ok and result.created and result.added == 2 and result.total == 2

    kopie = db.list_almanachs(session, "Nina")[0]
    assert kopie.name == "Star Wars"
    assert kopie.target_playlist_name == "Plex Almanach – Nina · Star Wars"
    assert db.almanach_keys(session, kopie.id) == {"100", "2"}
    # Das Original bleibt unangetastet.
    assert len(db.list_almanachs(session, "Alex")) == 1


def test_copy_builds_against_the_other_profiles_watch_history(session, almanach, plex_data):
    """Ninas Playlist enthält nur, was Nina selbst noch nicht gesehen hat."""
    gateway, ninas_server = _nina_gateway(plex_data, watched_episode=True)
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    result = copy_almanach(session, almanach, "Nina", gateway=gateway)

    assert result.sync.ok
    assert result.sync.item_count == 1          # eine Folge hat Nina schon gesehen
    assert "Nina" in gateway.connections
    playlist = ninas_server.playlists()[0]
    assert playlist.title == "Plex Almanach – Nina · Star Wars"
    assert [i.title for i in playlist.items()] == ["Folge 2"]

    # Alex' eigene Playlist bleibt davon unberührt.
    assert gateway.server.playlists() == []


def test_copying_twice_does_not_duplicate(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    copy_almanach(session, almanach, "Nina", gateway=gateway)

    second = copy_almanach(session, almanach, "Nina", gateway=gateway)

    assert second.ok and second.unchanged and second.total == 1
    assert len(db.list_almanachs(session, "Nina")) == 1


def test_copying_again_tops_up_missing_entries(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    copy_almanach(session, almanach, "Nina", gateway=gateway)

    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    result = copy_almanach(session, almanach, "Nina", gateway=gateway)

    assert not result.created and result.added == 1 and result.total == 2
    assert db.almanach_keys(session, db.list_almanachs(session, "Nina")[0].id) == {"2", "100"}


def test_copy_keeps_entries_the_other_profile_added_itself(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    copy_almanach(session, almanach, "Nina", gateway=gateway)
    kopie = db.list_almanachs(session, "Nina")[0]
    db.add_to_almanach(session, kopie, "3", "movie", "Matrix")  # Ninas eigene Ergänzung

    copy_almanach(session, almanach, "Nina", gateway=gateway)

    assert db.almanach_keys(session, kopie.id) == {"2", "3"}


def test_copy_takes_the_cover_along(session, almanach, gateway, png_image):
    from app import covers

    almanach.cover_path = covers.store(covers.almanach_stem(almanach.id), png_image)
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    copy_almanach(session, almanach, "Nina", gateway=gateway)

    kopie = db.list_almanachs(session, "Nina")[0]
    assert kopie.cover_path == f"almanach-{kopie.id}.png"
    assert covers.path_for(kopie.cover_path).read_bytes() == png_image


def test_copy_to_the_owner_is_refused(session, almanach, gateway):
    result = copy_almanach(session, almanach, "Alex", gateway=gateway)

    assert not result.ok and "bereits" in result.error
    assert len(db.list_almanachs(session, "Alex")) == 1


def test_copy_without_building(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    result = copy_almanach(session, almanach, "Nina", gateway=gateway, build=False)

    assert result.sync is None
    assert gateway.server.playlists() == []


def test_copy_to_several_profiles(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    results = copy_almanach_to_users(session, almanach, ["Nina", "Alex"], gateway=gateway)

    assert [r.user_id for r in results] == ["Nina", "Alex"]
    assert results[0].ok and not results[1].ok  # Alex ist der Eigentümer


def test_copies_are_kept_up_to_date_by_the_scheduler(session, almanach, gateway):
    from app.almanach import sync_all_almanachs

    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    copy_almanach(session, almanach, "Nina", gateway=gateway, build=False)

    results = sync_all_almanachs(session, trigger="poll", gateway=gateway)

    assert sorted(r.playlist_name for r in results) == [
        "Plex Almanach – Alex · Star Wars",
        "Plex Almanach – Nina · Star Wars",
    ]
