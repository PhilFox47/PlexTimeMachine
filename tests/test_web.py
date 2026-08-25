"""Tests der Weboberfläche und der Endpunkte."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.plex_client import set_gateway


@pytest.fixture
def client(gateway):
    from app.main import app

    set_gateway(gateway)
    with TestClient(app) as test_client:
        yield test_client
    set_gateway(None)


def test_dashboard_renders_time_circuits(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert "Plex Time Machine" in body
    assert 'id="digits-start"' in body and 'id="digits-end"' in body
    assert "Zeitreise starten" in body
    # Nutzer-Umschalter enthält beide Home-User
    assert "Alex (Admin)" in body and "Nina" in body


def test_set_period_returns_preview_and_persists(client, session):
    response = client.post("/period", data={"start": "1985-01-01", "end": "1985-12-31"})

    assert response.status_code == 200
    assert "Zurück in die Zukunft" in response.text
    assert "Knight Rider" in response.text
    assert "Matrix" not in response.text  # ausserhalb des Zeitraums

    state = db.get_or_create_user_state(session, "Alex")
    assert state.current_date_start.isoformat() == "1985-01-01"


def test_set_period_rejects_garbage_dates(client):
    response = client.post("/period", data={"start": "nicht-ein-datum", "end": "1985-12-31"})

    assert response.status_code == 200
    assert "gültiges Start- und Enddatum" in response.text


def test_preview_fragment_endpoint(client):
    response = client.get("/preview", params={"start": "1999-01-01", "end": "1999-12-31"})

    assert response.status_code == 200
    assert "Matrix" in response.text


def test_blacklist_add_removes_item_from_preview(client, session):
    response = client.post(
        "/blacklist/add",
        data={
            "rating_key": "100",
            "media_type": "show",
            "title": "Knight Rider",
            "start": "1985-01-01",
            "end": "1985-12-31",
        },
    )

    assert response.status_code == 200
    assert "Knight Rider" not in response.text
    assert "Zurück in die Zukunft" in response.text
    assert db.blacklist_keys(session, "Alex") == {"100"}


def test_blacklist_page_and_release(client, session):
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")

    page = client.get("/blacklist")
    assert "Knight Rider" in page.text and "Serie" in page.text

    response = client.post(
        "/blacklist/remove", data={"rating_key": "100"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert db.blacklist_keys(session, "Alex") == set()


def test_sync_endpoint_builds_playlist(client, session, gateway):
    db.set_period(session, "Alex", __import__("datetime").date(1985, 1, 1),
                  __import__("datetime").date(1985, 12, 31))

    response = client.post("/sync")

    assert response.status_code == 200
    assert "Zeitreise abgeschlossen" in response.text
    playlist = gateway.server.playlists()[0]
    assert playlist.title == "Plex Time Machine – Alex"
    assert len(playlist.items()) == 5


def test_sync_without_period_reports_error(client):
    response = client.post("/sync")

    assert "Zeitreise abgebrochen" in response.text
    assert "Zeitraum" in response.text


def test_logbook_lists_journeys(client, session):
    import datetime

    db.set_period(session, "Alex", datetime.date(1985, 1, 1), datetime.date(1985, 12, 31))
    client.post("/sync")

    response = client.get("/logbook")

    assert "01.01.1985" in response.text
    assert "manual" in response.text


def test_user_switch_sets_cookie(client, session):
    response = client.post("/user/select", data={"user": "Nina"}, follow_redirects=False)

    assert response.status_code == 303
    assert client.cookies.get("ptm_user") == "Nina"

    client.post("/period", data={"start": "1985-01-01", "end": "1985-12-31"})
    assert db.get_or_create_user_state(session, "Nina").has_period


def test_webhook_schedules_sync_for_relevant_event(client):
    payload = json.dumps({"event": "media.scrobble"})
    response = client.post("/webhook/plex", files={"payload": (None, payload)})

    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"


def test_webhook_ignores_irrelevant_event(client):
    response = client.post("/webhook/plex", json={"event": "media.play"})

    assert response.json() == {"status": "ignored", "event": "media.play"}


def test_webhook_rejects_wrong_token(client, monkeypatch):
    from app import config, main

    monkeypatch.setenv("PTM_WEBHOOK_TOKEN", "geheim")
    config.get_settings.cache_clear()
    try:
        response = client.post("/webhook/plex", json={"event": "media.scrobble"})
        assert response.status_code == 401

        ok = client.post("/webhook/plex?token=geheim", json={"event": "media.scrobble"})
        assert ok.status_code == 200
    finally:
        monkeypatch.delenv("PTM_WEBHOOK_TOKEN", raising=False)
        config.get_settings.cache_clear()


def test_thumb_proxy_rejects_foreign_paths(client):
    for bad in ("http://evil.example/x.png", "//evil.example/x.png", "/etc/passwd"):
        assert client.get("/thumb", params={"path": bad}).status_code == 400


def test_thumb_proxy_streams_plex_image(client, monkeypatch, gateway):
    class FakeResponse:
        status_code = 200
        content = b"\x89PNG-bytes"
        headers = {"content-type": "image/png"}

    gateway.server.url = lambda path, includeToken=False: "http://plex.test" + path
    monkeypatch.setattr("app.main.requests.get", lambda url, timeout=10: FakeResponse())

    response = client.get("/thumb", params={"path": "/library/metadata/1/thumb/1"})

    assert response.status_code == 200
    assert response.content == b"\x89PNG-bytes"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400"


def test_healthz(client):
    body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert "version" in body


# ---------------------------------------------------------------------------
# Wochenschritte und Wochentagsanzeige
# ---------------------------------------------------------------------------


def test_preview_shows_weekday_next_to_date(client):
    response = client.post("/period", data={"start": "2000-01-31", "end": "2000-02-07"})

    assert "Mi 02.02.2000" in response.text  # Magnolia, Mittwoch


def test_dashboard_restores_remembered_period(client):
    """Der zuletzt gewählte Zeitraum überlebt den Seitenwechsel."""
    client.post("/period", data={"start": "2000-01-31", "end": "2000-02-07"})

    body = client.get("/").text

    assert 'value="2000-01-31"' in body and 'value="2000-02-07"' in body
    assert body.count('<span class="weekday">Mo</span>') == 2  # beide Zeit-Displays


def test_dashboard_does_not_wait_for_the_preview(client):
    """Die Seite kommt sofort; die Vorschau holt sie sich danach selbst.

    Sonst hängt der Seitenaufbau bei großen Bibliotheken an Plex und die
    Oberfläche wirkt tot.
    """
    client.post("/period", data={"start": "2000-01-31", "end": "2000-02-07"})

    body = client.get("/").text

    assert 'hx-get="/preview?start=2000-01-31&amp;end=2000-02-07"' in body
    assert 'hx-trigger="load"' in body
    assert "Magnolia" not in body  # noch nicht enthalten ...

    nachgeladen = client.get(
        "/preview", params={"start": "2000-01-31", "end": "2000-02-07"}
    ).text
    assert "Magnolia" in nachgeladen  # ... sondern kommt mit dem Nachladen


def test_dashboard_offers_week_stepping(client):
    body = client.get("/").text

    assert 'data-shift="1"' in body and "Nächste Woche" in body
    assert 'data-shift="-1"' in body
    assert 'id="snap-week"' in body


def test_default_period_for_new_user_is_a_week():
    from app.main import default_period

    start, end = default_period()

    assert start.weekday() == end.weekday() == 0  # Montag bis Montag
    assert (end - start).days == 7


def test_logbook_shows_weekdays(client, session):
    import datetime

    db.set_period(session, "Alex", datetime.date(2000, 1, 31), datetime.date(2000, 2, 7))
    client.post("/sync")

    body = client.get("/logbook").text

    assert "Mo 31.01.2000 – Mo 07.02.2000" in body


def test_blacklist_page_shows_weekday(client, session):
    db.add_to_blacklist(session, "Alex", "100", "show", "Knight Rider")

    body = client.get("/blacklist").text
    entry = db.list_blacklist(session, "Alex")[0]

    assert entry.added_at.strftime("%d.%m.%Y") in body
    assert any(day in body for day in ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"))


# ---------------------------------------------------------------------------
# Almanach
# ---------------------------------------------------------------------------


@pytest.fixture
def almanach(session):
    return db.create_almanach(session, "Alex", "Star Wars")


def test_almanach_overview_lists_collections(client, session, almanach):
    body = client.get("/almanach").text

    assert "Star Wars" in body
    assert 'action="/almanach/new"' in body  # Formular zum Anlegen
    assert f'href="/almanach/{almanach.id}"' in body


def test_creating_a_collection_leads_to_its_page(client, session):
    response = client.post("/almanach/new", data={"name": "Achtziger"}, follow_redirects=False)

    assert response.status_code == 303
    created = db.list_almanachs(session, "Alex")[0]
    assert created.name == "Achtziger"
    assert response.headers["location"] == f"/almanach/{created.id}"
    share = db.get_share(session, created.id, "Alex")
    assert share.target_playlist_name == "Plex Almanach – Alex · Achtziger"


def test_detail_page_shows_name_and_search(client, almanach):
    body = client.get(f"/almanach/{almanach.id}").text

    assert "Star Wars" in body and 'name="q"' in body
    assert "Noch nichts aufgenommen" in body
    assert "Plex Almanach – Alex · Star Wars" in body


def test_detail_page_of_another_user_is_not_reachable(client, session):
    fremd = db.create_almanach(session, "Nina", "Ninas Sammlung")

    assert client.get(f"/almanach/{fremd.id}").status_code == 404
    assert client.post(f"/almanach/{fremd.id}/remove", data={"rating_key": "1"}).status_code == 404


def test_almanach_search_lists_hits_with_add_button(client, almanach):
    body = client.get(f"/almanach/{almanach.id}/search", params={"q": "rider"}).text

    assert "Knight Rider" in body
    assert f'hx-post="/almanach/{almanach.id}/add"' in body
    assert "2 Episoden" in body


def test_almanach_search_without_query_stays_quiet(client, almanach):
    assert client.get(f"/almanach/{almanach.id}/search", params={"q": ""}).text.strip() == ""


def test_almanach_add_and_remove_update_the_stock(client, session, almanach):
    added = client.post(
        f"/almanach/{almanach.id}/add",
        data={"rating_key": "100", "media_type": "show", "title": "Knight Rider", "year": "1982"},
    )

    assert "Knight Rider" in added.text and "Serie" in added.text
    entry = db.list_almanach_entries(session, almanach.id)[0]
    assert entry.plex_rating_key == "100" and entry.year == 1982

    removed = client.post(f"/almanach/{almanach.id}/remove", data={"rating_key": "100"})

    assert "Noch nichts aufgenommen" in removed.text
    assert db.list_almanach_entries(session, almanach.id) == []


def test_almanach_add_is_idempotent(client, session, almanach):
    for _ in range(2):
        client.post(
            f"/almanach/{almanach.id}/add",
            data={"rating_key": "100", "media_type": "show", "title": "Knight Rider"},
        )

    assert len(db.list_almanach_entries(session, almanach.id)) == 1


def test_almanach_preview_shows_release_order(client, session, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    db.add_to_almanach(session, almanach, "1", "movie", "Zurück in die Zukunft")

    body = client.get(f"/almanach/{almanach.id}/preview").text

    # Film vom 03.07.1985 steht vor den Episoden vom 20.09.1985
    assert body.index("Zurück in die Zukunft") < body.index("Pilot")
    assert "Mi 03.07.1985" in body


def test_almanach_sync_builds_playlist(client, session, gateway, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    response = client.post(f"/almanach/{almanach.id}/sync")

    assert "Alex" in response.text and "ungesehene Titel" in response.text
    playlist = gateway.server.playlists()[0]
    assert playlist.title == "Plex Almanach – Alex · Star Wars"
    assert len(playlist.items()) == 2


def test_almanach_sync_without_entries_reports_error(client, almanach):
    response = client.post(f"/almanach/{almanach.id}/sync")

    assert "leer" in response.text


def test_rename_and_delete_a_collection(client, session, almanach, gateway):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    client.post(f"/almanach/{almanach.id}/sync")

    renamed = client.post(
        f"/almanach/{almanach.id}/rename", data={"name": "Star Wars komplett"},
        follow_redirects=False,
    )
    assert renamed.status_code == 303
    session.expire_all()
    assert db.list_almanachs(session, "Alex")[0].name == "Star Wars komplett"
    # Die bestehende Plex-Playlist wird mit umbenannt statt verwaist zu bleiben.
    assert [p.title for p in gateway.server.playlists()] == [
        "Plex Almanach – Alex · Star Wars komplett"
    ]

    deleted = client.post(f"/almanach/{almanach.id}/delete", follow_redirects=False)

    assert deleted.status_code == 303
    assert db.list_almanachs(session, "Alex") == []
    assert gateway.server.playlists() == []  # Playlist wird mit entfernt


def test_stock_update_carries_the_build_button_state(client, session, almanach):
    """Der Auslöse-Knopf darf nach dem ersten Eintrag nicht deaktiviert bleiben."""
    empty = client.get(f"/almanach/{almanach.id}").text
    assert '<button class="btn-launch" type="submit" disabled>' in empty

    added = client.post(
        f"/almanach/{almanach.id}/add",
        data={"rating_key": "100", "media_type": "show", "title": "Knight Rider"},
    ).text

    assert 'hx-swap-oob="true"' in added  # Knopf reist per Out-of-band-Swap mit
    assert '<button class="btn-launch" type="submit" >' in added

    removed = client.post(f"/almanach/{almanach.id}/remove", data={"rating_key": "100"}).text
    assert '<button class="btn-launch" type="submit" disabled>' in removed


def test_logbook_separates_almanach_from_time_machine(client, session, almanach):
    import datetime

    db.set_period(session, "Alex", datetime.date(1985, 1, 1), datetime.date(1985, 12, 31))
    client.post("/sync")
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    client.post(f"/almanach/{almanach.id}/sync")

    body = client.get("/logbook").text

    assert "Almanach" in body and "Zeitreise" in body
    assert "nach Auswahl" in body  # Almanach-Zeile ohne Zeitraum
    assert "Star Wars" in body     # Notiz nennt die Sammlung


# ---------------------------------------------------------------------------
# Watch-Status zurücksetzen (zweistufige Bestätigung)
# ---------------------------------------------------------------------------


def test_reset_step_one_only_asks(client, session, gateway, plex_data, almanach):
    plex_data["episodes"][0].viewCount = 1
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    body = client.get(f"/almanach/{almanach.id}/reset").text

    assert "Wirklich zurücksetzen" in body
    assert "1</strong> gesehene" in body
    assert "Ja, 1 Titel zurücksetzen" in body
    assert "hx-confirm=" in body                       # zweite Rückfrage
    assert plex_data["episodes"][0].viewCount == 1     # noch nichts passiert


def test_reset_needs_the_explicit_confirmation(client, session, plex_data, almanach):
    plex_data["episodes"][0].viewCount = 1
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    body = client.post(f"/almanach/{almanach.id}/reset", data={"confirm": "vielleicht"}).text

    assert "Wirklich zurücksetzen" in body             # fragt erneut
    assert plex_data["episodes"][0].viewCount == 1     # unverändert


def test_reset_executes_and_rebuilds_the_playlist(client, session, gateway, plex_data, almanach):
    plex_data["episodes"][0].viewCount = 1
    plex_data["episodes"][1].viewCount = 1
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    body = client.post(f"/almanach/{almanach.id}/reset", data={"confirm": "ja"}).text

    assert "Zurückgesetzt" in body and "2 Episoden" in body
    assert "Playlist neu gebaut" in body
    assert [e.viewCount for e in plex_data["episodes"][:2]] == [0, 0]
    assert len(gateway.server.playlists()[0].items()) == 2


def test_reset_reports_when_nothing_is_watched(client, session, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    body = client.get(f"/almanach/{almanach.id}/reset").text

    assert "bereits alles als ungesehen" in body
    assert "Ja," not in body  # kein Ausführen-Knopf


# ---------------------------------------------------------------------------
# Cover-Bilder
# ---------------------------------------------------------------------------


def test_cover_upload_reaches_plex_and_is_shown(client, session, gateway, almanach, png_image):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    client.post(f"/almanach/{almanach.id}/sync")  # Playlist existiert schon

    response = client.post(
        f"/almanach/{almanach.id}/cover",
        files={"cover": ("poster.png", png_image, "image/png")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/almanach/{almanach.id}?cover=uebertragen"
    assert gateway.server.playlists()[0].posters == [png_image]

    session.expire_all()
    stored = db.get_almanach(session, "Alex", almanach.id)
    assert stored.cover_path == f"almanach-{almanach.id}.png"
    assert db.get_share(session, almanach.id, "Alex").cover_applied_at is not None

    # Die Vorschau liefert genau das hochgeladene Bild aus.
    image = client.get(f"/almanach/{almanach.id}/cover/image")
    assert image.status_code == 200
    assert image.content == png_image
    assert image.headers["content-type"] == "image/png"


def test_cover_upload_without_playlist_is_kept_for_later(client, session, almanach, png_image):
    response = client.post(
        f"/almanach/{almanach.id}/cover",
        files={"cover": ("poster.png", png_image, "image/png")},
        follow_redirects=False,
    )

    assert response.headers["location"] == f"/almanach/{almanach.id}?cover=gespeichert"
    session.expire_all()
    stored = db.get_almanach(session, "Alex", almanach.id)
    assert stored.cover_path
    assert db.get_share(session, almanach.id, "Alex").cover_applied_at is None


def test_cover_upload_rejects_non_images(client, session, almanach):
    response = client.post(
        f"/almanach/{almanach.id}/cover",
        files={"cover": ("schadhaft.png", b"<html>kein Bild</html>", "image/png")},
        follow_redirects=False,
    )

    assert "cover_error" in response.headers["location"]
    session.expire_all()
    assert db.get_almanach(session, "Alex", almanach.id).cover_path is None


def test_cover_notice_is_rendered_on_the_page(client, almanach):
    body = client.get(f"/almanach/{almanach.id}", params={"cover_error": "Zu groß."}).text

    assert "Cover nicht gesetzt" in body and "Zu groß." in body


def test_cover_delete_removes_file_and_poster(client, session, gateway, almanach, png_image):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    client.post(f"/almanach/{almanach.id}/sync")
    client.post(
        f"/almanach/{almanach.id}/cover",
        files={"cover": ("poster.png", png_image, "image/png")},
    )

    response = client.post(f"/almanach/{almanach.id}/cover/delete", follow_redirects=False)

    assert response.headers["location"] == f"/almanach/{almanach.id}?cover=entfernt"
    session.expire_all()
    assert db.get_almanach(session, "Alex", almanach.id).cover_path is None
    assert gateway.server.playlists()[0].poster_deleted is True
    assert client.get(f"/almanach/{almanach.id}/cover/image").status_code == 404


def test_cover_of_another_user_is_not_reachable(client, session, png_image):
    fremd = db.create_almanach(session, "Nina", "Ninas Sammlung")

    assert client.get(f"/almanach/{fremd.id}/cover/image").status_code == 404
    assert client.post(
        f"/almanach/{fremd.id}/cover",
        files={"cover": ("poster.png", png_image, "image/png")},
    ).status_code == 404


def test_timemachine_cover_upload_and_delete(client, session, gateway, png_image):
    import datetime

    db.set_period(session, "Alex", datetime.date(1985, 1, 1), datetime.date(1985, 12, 31))
    client.post("/sync")

    response = client.post(
        "/cover/timemachine",
        files={"cover": ("poster.png", png_image, "image/png")},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/?cover=uebertragen"
    assert gateway.server.playlists()[0].posters == [png_image]
    session.expire_all()
    assert db.get_or_create_user_state(session, "Alex").cover_path is not None

    assert client.get("/cover/timemachine/image").content == png_image

    client.post("/cover/timemachine/delete")
    session.expire_all()
    assert db.get_or_create_user_state(session, "Alex").cover_path is None


def test_dashboard_and_overview_show_the_cover_controls(client, session, almanach, png_image):
    dashboard = client.get("/").text
    assert 'action="/cover/timemachine"' in dashboard and 'name="cover"' in dashboard

    client.post(
        f"/almanach/{almanach.id}/cover",
        files={"cover": ("poster.png", png_image, "image/png")},
    )
    overview = client.get("/almanach").text
    assert f'src="/almanach/{almanach.id}/cover/image"' in overview


# ---------------------------------------------------------------------------
# Sammlung in andere Profile übernehmen
# ---------------------------------------------------------------------------


def test_share_panel_lists_the_other_profiles(client, almanach):
    body = client.get(f"/almanach/{almanach.id}").text

    assert "Für andere Profile" in body
    # Nur die Auswahlkästchen betrachten – "Alex" steht auch im Nutzer-Umschalter.
    assert 'name="profiles" value="Nina"' in body
    assert 'name="profiles" value="Alex"' not in body   # der Eigentümer nicht
    assert "nicht freigegeben" in body


def test_share_gives_the_other_profile_its_own_playlist(client, session, gateway, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    response = client.post(f"/almanach/{almanach.id}/share", data={"profiles": ["Nina"]})

    assert response.status_code == 200
    assert "freigegeben" in response.text
    assert "Plex Almanach – Nina · Star Wars" in response.text

    # Eine Sammlung, zwei Playlists – keine Kopie des Inhalts.
    assert len(db.list_almanachs(session, "Alex")) == 1
    assert sorted(s.plex_user_id for s in db.list_shares(session, almanach.id)) == ["Alex", "Nina"]


def test_shared_profile_sees_the_collection_read_only(client, session, gateway, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    client.post(f"/almanach/{almanach.id}/share", data={"profiles": ["Nina"]})

    client.post("/user/select", data={"user": "Nina"})
    body = client.get(f"/almanach/{almanach.id}").text

    assert "Freigegeben von" in body and "Alex" in body
    assert "Knight Rider" in body                  # Inhalt sichtbar
    assert 'name="q"' not in body                  # aber keine Suche
    assert "Almanach löschen" not in body          # und kein Löschen

    # Schreibende Zugriffe sind für das Gastprofil gesperrt.
    assert client.post(
        f"/almanach/{almanach.id}/add",
        data={"rating_key": "1", "media_type": "movie", "title": "Fremd"},
    ).status_code == 403
    assert client.post(
        f"/almanach/{almanach.id}/rename", data={"name": "Umbenannt"}
    ).status_code == 403
    assert client.post(f"/almanach/{almanach.id}/delete").status_code == 403
    assert db.almanach_keys(session, almanach.id) == {"100"}


def test_content_change_reaches_the_shared_profile(client, session, gateway, almanach):
    """Der Eigentümer ändert den Inhalt – die andere Playlist zieht mit."""
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    client.post(f"/almanach/{almanach.id}/share", data={"profiles": ["Nina"]})
    assert db.get_share(session, almanach.id, "Nina").last_item_count == 1

    client.post(
        f"/almanach/{almanach.id}/add",
        data={"rating_key": "100", "media_type": "show", "title": "Knight Rider"},
    )
    response = client.post(f"/almanach/{almanach.id}/sync")

    assert "Alex" in response.text and "Nina" in response.text  # baut für beide
    session.expire_all()
    assert db.get_share(session, almanach.id, "Nina").last_item_count == 3


def test_revoking_a_share_removes_access(client, session, gateway, almanach):
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    client.post(f"/almanach/{almanach.id}/share", data={"profiles": ["Nina"]})

    response = client.post(
        f"/almanach/{almanach.id}/share/revoke", data={"profile": "Nina"}
    )

    assert "zurückgenommen" in response.text
    assert db.get_share(session, almanach.id, "Nina") is None
    assert db.list_almanachs(session, "Nina") == []


def test_share_panel_marks_existing_shares(client, session, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")
    client.post(f"/almanach/{almanach.id}/share", data={"profiles": ["Nina"]})

    body = client.get(f"/almanach/{almanach.id}").text

    assert "freigegeben – eigene Playlist" in body


def test_share_without_a_profile_says_so(client, almanach):
    response = client.post(f"/almanach/{almanach.id}/share", data={})

    assert "Kein Profil gewählt" in response.text


def test_share_ignores_unknown_profiles(client, session, almanach):
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    response = client.post(
        f"/almanach/{almanach.id}/share", data={"profiles": ["Eindringling"]}
    )

    assert "Kein Profil gewählt" in response.text
    assert db.list_almanachs(session, "Eindringling") == []


def test_share_of_a_foreign_collection_is_refused(client, session):
    fremd = db.create_almanach(session, "Nina", "Ninas Sammlung")

    response = client.post(f"/almanach/{fremd.id}/share", data={"profiles": ["Alex"]})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Vertippte Jahreszahlen
# ---------------------------------------------------------------------------


def test_period_rejects_implausible_years(client, session):
    """Ein halb getipptes Jahr darf nicht gespeichert werden.

    Das Formular speichert bei jeder Änderung; beim Tippen entsteht dabei
    kurzzeitig etwa "0200-02-07". Landet das im Zeitraum, weist Plex den
    Datumsfilter ab und jede Suche wird zum Vollscan der Bibliothek.
    """
    client.post("/period", data={"start": "1985-01-01", "end": "1985-12-31"})

    response = client.post("/period", data={"start": "0200-02-07", "end": "0200-02-14"})

    assert "Jahr muss mindestens 1870 sein" in response.text
    state = db.get_or_create_user_state(session, "Alex")
    assert state.current_date_start.year == 1985  # der gute Zeitraum bleibt stehen


def test_dashboard_recovers_from_a_broken_stored_period(client, session):
    import datetime

    db.set_period(session, "Alex", datetime.date(200, 2, 7), datetime.date(200, 2, 14))

    body = client.get("/").text

    assert "unbrauchbar" in body and "Bitte neu wählen" in body
    assert 'value="0200-02-07"' not in body  # es steht ein brauchbarer Vorschlag drin
