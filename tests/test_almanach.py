"""Tests für Suche, Zusammenstellung und Playlist-Bau des Almanachs."""

from __future__ import annotations

from datetime import date

import pytest

from app import db
from app.almanach import (
    build_preview,
    collect_almanach_items,
    plan_reset,
    reset_watch_state,
    revoke_share,
    search_titles,
    share_with_users,
    sync_all_almanachs,
    sync_collection,
    sync_share,
)


@pytest.fixture
def almanach(session):
    return db.create_almanach(session, "Alex", "Star Wars")


@pytest.fixture
def share(session, almanach):
    """Die Playlist-Zeile des Eigentümers."""
    return db.get_or_create_share(session, almanach, "Alex")

# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def test_search_finds_shows_and_movies_by_partial_title(session, gateway, almanach):
    result = search_titles(session, almanach, "rider", "Alex", gateway=gateway)

    assert result.ok
    assert [(h.title, h.media_type) for h in result.hits] == [("Knight Rider", "show")]
    assert result.hits[0].episode_count == 2  # Serie meldet ihren Umfang


def test_search_is_case_insensitive_and_matches_movies(session, gateway, almanach):
    result = search_titles(session, almanach, "BRAZIL", "Alex", gateway=gateway)

    assert [h.title for h in result.hits] == ["Brazil"]
    assert result.hits[0].media_type == "movie"
    assert result.hits[0].year == 1985


def test_search_marks_entries_already_in_almanach(session, gateway, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    result = search_titles(session, almanach, "rider", "Alex", gateway=gateway)

    assert result.hits[0].already_added is True


def test_search_without_query_returns_nothing(session, gateway, almanach):
    assert search_titles(session, almanach, "   ", "Alex", gateway=gateway).hits == []


def test_search_reports_plex_errors(session, almanach):
    from app.plex_client import PlexUnavailable

    class BrokenGateway:
        def connect_as(self, user_id):
            raise PlexUnavailable("Server offline")

    result = search_titles(session, almanach, "star", "Alex", gateway=BrokenGateway())

    assert not result.ok and "offline" in result.error


def test_search_respects_limit(session, gateway, almanach):
    result = search_titles(session, almanach, "", "Alex", gateway=gateway, limit=1)

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


def test_preview_counts_movies_and_episodes(session, gateway, almanach, share):
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    preview = build_preview(session, share, gateway=gateway)

    assert preview.ok
    assert (preview.total, preview.movies, preview.episodes) == (3, 1, 2)


def test_preview_is_empty_without_entries(session, almanach, gateway, share):
    preview = build_preview(session, share, gateway=gateway)

    assert preview.ok and preview.total == 0


def test_preview_truncates_to_limit(session, gateway, almanach, share):
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    preview = build_preview(session, share, gateway=gateway, limit=2)

    assert preview.truncated and len(preview.items) == 2 and preview.total == 3


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


def test_sync_writes_own_playlist_and_logs_journey(session, gateway, almanach, share):
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("1", "movie", "Zurück in die Zukunft"))

    result = sync_share(session, share, trigger="manual", gateway=gateway)

    assert result.ok and result.item_count == 3
    assert result.playlist_name == "Star Wars – Alex – Almanach"

    playlist = gateway.server.playlists()[0]
    assert playlist.title == "Star Wars – Alex – Almanach"
    assert len(playlist.items()) == 3

    session.refresh(share)
    assert share.last_item_count == 3 and share.last_synced_at is not None

    journey = db.list_journeys(session, "Alex")[0]
    assert journey.kind == "almanach" and journey.item_count == 3
    assert journey.date_start is None  # der Almanach kennt keinen Zeitraum


def test_sync_requires_entries(session, almanach, gateway, share):
    result = sync_share(session, share, gateway=gateway)

    assert not result.ok and "leer" in result.error
    assert db.list_journeys(session, "Alex") == []


def test_sync_ignores_blacklist_because_selection_is_explicit(session, gateway, almanach, share):
    """Wer eine Serie bewusst in den Almanach legt, will sie auch sehen."""
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    result = sync_share(session, share, gateway=gateway)

    assert result.item_count == 2


def test_sync_reports_missing_entries_in_message(session, gateway, almanach, share):
    _entries(session, almanach, ("2", "movie", "Brazil"), ("999", "movie", "Weg damit"))

    result = sync_share(session, share, gateway=gateway)

    assert result.ok and result.item_count == 1
    assert "Weg damit" in result.message
    assert "1 Einträge nicht mehr" in db.list_journeys(session, "Alex")[0].note


def test_sync_uses_the_owning_user_context(session, gateway):
    nina = db.create_almanach(session, "Nina", "Brazil-Abend")
    db.add_to_almanach(session, nina, "2", "movie", "Brazil")

    sync_share(session, db.get_or_create_share(session, nina, "Nina"), gateway=gateway)

    assert gateway.connections == ["Nina"]


def test_sync_all_almanachs_covers_every_filled_collection(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    zweiter = db.create_almanach(session, "Alex", "Achtziger")
    db.add_to_almanach(session, zweiter, "1", "movie", "Zurück in die Zukunft")
    db.create_almanach(session, "Alex", "Leer")  # ohne Bestand

    results = sync_all_almanachs(session, trigger="poll", gateway=gateway)

    assert sorted(r.playlist_name for r in results) == [
        "Achtziger – Alex – Almanach",
        "Star Wars – Alex – Almanach",
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
    assert titles == ["Plex Time Machine – Alex", "Star Wars – Alex – Almanach"]
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
    assert (
        db.get_share(session, star_wars.id, "Alex").target_playlist_name
        != db.get_share(session, achtziger.id, "Alex").target_playlist_name
    )


def test_get_almanach_refuses_other_users(session):
    fremd = db.create_almanach(session, "Nina", "Ninas Sammlung")

    assert db.get_almanach(session, "Alex", fremd.id) is None
    assert db.get_almanach(session, "Nina", fremd.id) is not None


def test_rename_updates_the_playlist_name_of_every_profile(session, almanach, gateway):
    share_with_users(session, almanach, ["Nina"], gateway=gateway, build=False)

    db.rename_almanach(session, almanach, "Star Wars komplett")

    assert almanach.name == "Star Wars komplett"
    assert sorted(s.target_playlist_name for s in db.list_shares(session, almanach.id)) == [
        "Star Wars komplett – Alex – Almanach",
        "Star Wars komplett – Nina – Almanach",
    ]


def test_playlist_name_always_follows_the_template(session, almanach, share):
    """Der Playlist-Name wird aus der Vorlage abgeleitet, nicht eingefroren."""
    share.target_playlist_name = "Irgendein alter Name"
    session.add(share)
    session.commit()

    db.rename_almanach(session, almanach, "Neuer Name")

    session.refresh(share)
    assert share.target_playlist_name == "Neuer Name – Alex – Almanach"


def test_sync_renames_a_playlist_that_still_carries_the_old_name(
    session, almanach, share, gateway
):
    """Nach einer Schema-Änderung wandert die vorhandene Playlist mit.

    Sonst bliebe sie unter dem alten Namen liegen und der nächste Lauf legte
    eine zweite daneben an.
    """
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    gateway.server.createPlaylist("Plex Almanach – Alex · Star Wars", items=[])
    share.target_playlist_name = "Plex Almanach – Alex · Star Wars"
    session.add(share)
    session.commit()

    ergebnis = sync_share(session, share, gateway=gateway)

    assert ergebnis.ok
    assert [p.title for p in gateway.server.playlists()] == ["Star Wars – Alex – Almanach"]
    assert share.target_playlist_name == "Star Wars – Alex – Almanach"


def test_delete_removes_collection_entries_and_shares(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    share_with_users(session, almanach, ["Nina"], gateway=gateway, build=False)

    db.delete_almanach(session, almanach)

    assert db.list_almanachs(session, "Alex") == []
    assert db.list_almanachs(session, "Nina") == []
    assert db.list_almanach_entries(session, almanach.id) == []
    assert db.list_shares(session, almanach.id) == []


def test_delete_playlist_removes_the_plex_playlist(session, almanach, share, gateway):
    from app.almanach import delete_playlist

    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    sync_share(session, share, gateway=gateway)
    assert gateway.server.playlists()

    assert delete_playlist(share, gateway=gateway) is True
    assert gateway.server.playlists() == []


# ---------------------------------------------------------------------------
# Watch-Status zurücksetzen
# ---------------------------------------------------------------------------


def test_plan_reset_counts_watched_items(session, gateway, plex_data, almanach, share):
    plex_data["episodes"][0].viewCount = 1        # eine Knight-Rider-Folge gesehen
    plex_data["movies"][1].viewCount = 1          # Brazil gesehen
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    plan = plan_reset(session, share, gateway=gateway, name=almanach.name)

    assert plan.ok
    assert (plan.watched_episodes, plan.watched_movies) == (1, 1)
    assert (plan.total_episodes, plan.total_movies) == (2, 1)
    assert plan.watched_total == 2
    assert not plan.nothing_to_do


def test_plan_reset_knows_when_nothing_is_watched(session, gateway, almanach, share):
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    plan = plan_reset(session, share, gateway=gateway, name=almanach.name)

    assert plan.nothing_to_do and plan.watched_total == 0


def test_plan_reset_requires_entries(session, gateway, almanach, share):
    plan = plan_reset(session, share, gateway=gateway, name=almanach.name)

    assert not plan.ok and "leer" in plan.error


def test_plan_reset_does_not_change_anything(session, gateway, plex_data, almanach, share):
    plex_data["episodes"][0].viewCount = 1
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    plan_reset(session, share, gateway=gateway, name=almanach.name)

    assert plex_data["episodes"][0].viewCount == 1  # unverändert


def test_reset_marks_everything_unplayed(session, gateway, plex_data, almanach, share):
    plex_data["episodes"][0].viewCount = 1
    plex_data["episodes"][1].viewCount = 3
    plex_data["movies"][1].viewCount = 1
    _entries(session, almanach, ("100", "show", "Knight Rider"), ("2", "movie", "Brazil"))

    result = reset_watch_state(session, share, gateway=gateway, name=almanach.name)

    assert result.ok
    assert (result.episodes, result.movies, result.total) == (2, 1, 3)
    assert [e.viewCount for e in plex_data["episodes"][:2]] == [0, 0]
    assert plex_data["movies"][1].viewCount == 0


def test_reset_only_touches_its_own_collection(session, gateway, plex_data, almanach, share):
    plex_data["movies"][0].viewCount = 1          # Zurück in die Zukunft, nicht im Bestand
    plex_data["movies"][1].viewCount = 1          # Brazil, im Bestand
    _entries(session, almanach, ("2", "movie", "Brazil"))

    reset_watch_state(session, share, gateway=gateway, name=almanach.name)

    assert plex_data["movies"][0].viewCount == 1  # bleibt gesehen
    assert plex_data["movies"][1].viewCount == 0


def test_reset_skips_missing_entries(session, gateway, almanach, share):
    _entries(session, almanach, ("999", "movie", "Weg damit"), ("2", "movie", "Brazil"))

    result = reset_watch_state(session, share, gateway=gateway, name=almanach.name)

    assert result.ok and result.missing == ["Weg damit"]


def test_reset_requires_entries(session, gateway, almanach, share):
    result = reset_watch_state(session, share, gateway=gateway, name=almanach.name)

    assert not result.ok and "leer" in result.error


def test_reset_reports_plex_errors(session, almanach, share):
    from app.plex_client import PlexUnavailable

    class BrokenGateway:
        def connect_as(self, user_id):
            raise PlexUnavailable("Server offline")

    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    result = reset_watch_state(session, share, gateway=BrokenGateway(), name=almanach.name)

    assert not result.ok and "offline" in result.error


def test_reset_makes_items_return_to_the_playlist(session, gateway, plex_data, almanach, share):
    """Nach dem Reset gehört alles wieder in die Playlist."""
    plex_data["episodes"][0].viewCount = 1
    plex_data["episodes"][1].viewCount = 1
    _entries(session, almanach, ("100", "show", "Knight Rider"))

    assert sync_share(session, share, gateway=gateway).item_count == 0

    reset_watch_state(session, share, gateway=gateway, name=almanach.name)

    assert sync_share(session, share, gateway=gateway).item_count == 2


# ---------------------------------------------------------------------------
# Gemeinsamer Inhalt, eigener Fortschritt
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


def test_sharing_gives_the_other_profile_its_own_playlist(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    results = share_with_users(session, almanach, ["Nina"], gateway=gateway)

    assert results[0].ok and results[0].added
    # Es gibt weiterhin genau eine Sammlung – nur eine zweite Playlist.
    assert len(db.list_almanachs(session, "Alex")) == 1
    assert [a.id for a in db.list_almanachs(session, "Nina")] == [almanach.id]
    assert sorted(s.plex_user_id for s in db.list_shares(session, almanach.id)) == ["Alex", "Nina"]
    assert db.get_share(session, almanach.id, "Nina").target_playlist_name == (
        "Star Wars – Nina – Almanach"
    )


def test_shared_content_changes_reach_every_profile(session, almanach, gateway):
    """Der Kern: Inhalt einmal pflegen, alle Playlists ziehen mit."""
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    share_with_users(session, almanach, ["Nina"], gateway=gateway)
    ninas_share = db.get_share(session, almanach.id, "Nina")
    assert ninas_share.last_item_count == 1

    # Eigentümer nimmt eine Serie auf ...
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    sync_collection(session, almanach, gateway=gateway)

    session.refresh(ninas_share)
    assert ninas_share.last_item_count == 3          # Nina bekommt sie automatisch
    assert db.get_share(session, almanach.id, "Alex").last_item_count == 3

    # ... und wieder heraus.
    db.remove_from_almanach(session, almanach, "100")
    sync_collection(session, almanach, gateway=gateway)

    session.refresh(ninas_share)
    assert ninas_share.last_item_count == 1


def test_each_profile_keeps_its_own_watch_progress(session, almanach, plex_data):
    """Gleicher Inhalt, unterschiedlicher Fortschritt."""
    gateway, ninas_server = _nina_gateway(plex_data, watched_episode=True)
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    share_with_users(session, almanach, ["Nina"], gateway=gateway)
    sync_share(session, db.get_or_create_share(session, almanach, "Alex"), gateway=gateway)

    alex_playlist = gateway.server.playlists()[0]
    nina_playlist = ninas_server.playlists()[0]

    assert len(alex_playlist.items()) == 2                       # Alex hat nichts gesehen
    assert [i.title for i in nina_playlist.items()] == ["Folge 2"]  # Nina eine Folge
    assert alex_playlist.title == "Star Wars – Alex – Almanach"
    assert nina_playlist.title == "Star Wars – Nina – Almanach"


def test_sharing_twice_is_harmless(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    share_with_users(session, almanach, ["Nina"], gateway=gateway)

    second = share_with_users(session, almanach, ["Nina"], gateway=gateway)

    assert second[0].ok and not second[0].added
    assert len(db.list_shares(session, almanach.id)) == 2


def test_sharing_with_the_owner_is_refused(session, almanach, gateway):
    result = share_with_users(session, almanach, ["Alex"], gateway=gateway)[0]

    assert not result.ok and "bereits" in result.error
    assert len(db.list_shares(session, almanach.id)) == 1


def test_revoking_removes_access_and_playlist(session, almanach, gateway, plex_data):
    gateway, ninas_server = _nina_gateway(plex_data)
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    share_with_users(session, almanach, ["Nina"], gateway=gateway)
    assert ninas_server.playlists()

    assert revoke_share(session, almanach, "Nina", gateway=gateway) is True

    assert db.list_almanachs(session, "Nina") == []
    assert db.get_share(session, almanach.id, "Nina") is None
    assert ninas_server.playlists() == []           # Ninas Playlist ist weg
    assert db.almanach_keys(session, almanach.id) == {"2"}  # Inhalt bleibt


def test_revoking_the_owner_is_refused(session, almanach, gateway):
    assert revoke_share(session, almanach, "Alex", gateway=gateway) is False
    assert db.get_share(session, almanach.id, "Alex") is not None


def test_reset_of_one_profile_leaves_the_other_alone(session, almanach, plex_data):
    gateway, ninas_server = _nina_gateway(plex_data, watched_episode=True)
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    share_with_users(session, almanach, ["Nina"], gateway=gateway, build=False)
    plex_data["episodes"][0].viewCount = 1          # Alex hat auch etwas gesehen

    ninas_share = db.get_share(session, almanach.id, "Nina")
    result = reset_watch_state(session, ninas_share, gateway=gateway, name=almanach.name)

    assert result.ok and result.episodes == 1
    assert plex_data["episodes"][0].viewCount == 1  # Alex' Fortschritt bleibt


def test_sync_collection_builds_one_playlist_per_profile(session, almanach, plex_data):
    gateway, ninas_server = _nina_gateway(plex_data)
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    share_with_users(session, almanach, ["Nina"], gateway=gateway, build=False)

    results = sync_collection(session, almanach, gateway=gateway)

    assert sorted(r.user_id for r in results) == ["Alex", "Nina"]
    assert all(r.ok for r in results)
    assert gateway.server.playlists()[0].title == "Star Wars – Alex – Almanach"
    assert ninas_server.playlists()[0].title == "Star Wars – Nina – Almanach"


def test_scheduler_keeps_every_profile_up_to_date(session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    share_with_users(session, almanach, ["Nina"], gateway=gateway, build=False)

    results = sync_all_almanachs(session, trigger="poll", gateway=gateway)

    assert sorted(r.playlist_name for r in results) == [
        "Star Wars – Alex – Almanach",
        "Star Wars – Nina – Almanach",
    ]


def test_cover_belongs_to_the_collection_and_reaches_every_playlist(
    session, almanach, gateway, png_image, plex_data
):
    from app import covers

    gateway, ninas_server = _nina_gateway(plex_data)
    almanach.cover_path = covers.store(covers.almanach_stem(almanach.id), png_image)
    session.add(almanach)
    session.commit()
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    share_with_users(session, almanach, ["Nina"], gateway=gateway)
    sync_share(session, db.get_or_create_share(session, almanach, "Alex"), gateway=gateway)

    assert gateway.server.playlists()[0].posters == [png_image]
    assert ninas_server.playlists()[0].posters == [png_image]
