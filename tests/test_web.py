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
    assert "Magnolia" in body  # Vorschau wird direkt mitgeladen


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
