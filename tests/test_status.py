"""Tests der Statusanzeige für die Übergänge und des Protokolls."""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app import db, logbuffer, transition_build

ERA_START = date(1985, 1, 1)
ERA_END = date(1985, 12, 31)


@pytest.fixture
def uebergaenge_an(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setenv("PTM_TRANSITIONS_ENABLED", "true")
    monkeypatch.setenv("PTM_TRANSITION_DIR", str(tmp_path / "clips"))
    monkeypatch.setenv("PTM_TRANSITION_LIBRARY", "Zeitreise-Übergänge")
    monkeypatch.setenv("PTM_TRANSITION_USER", "Alex")
    config.get_settings.cache_clear()
    yield tmp_path / "clips"
    config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Auskunft
# ---------------------------------------------------------------------------


def test_the_panel_explains_that_transitions_are_off(client, session):
    """Ohne Übergänge steht da, welcher Schalter fehlt – nicht bloß nichts."""
    stand = transition_build.status(session, "Alex")

    assert not stand.aktiv
    assert "PTM_TRANSITIONS_ENABLED" in stand.checks[0].detail


def test_the_panel_names_the_profile_that_gets_clips(session, uebergaenge_an):
    """Der häufigste Grund für „es passiert nichts": das falsche Profil."""
    stand = transition_build.status(session, "Leo")

    assert not stand.aktiv
    hinweis = stand.checks[0]
    assert not hinweis.ok
    assert "Alex" in hinweis.detail and "Leo" in hinweis.detail


def test_the_panel_checks_folder_and_library(session, gateway, uebergaenge_an, tmp_path):
    gateway.server.mit_uebergaengen(tmp_path / "clips")

    stand = transition_build.status(session, "Alex", gateway.server)

    geprueft = {c.name: c for c in stand.checks}
    assert geprueft["Profil"].ok
    assert geprueft["Ordner"].ok
    assert geprueft["Plex-Bibliothek"].ok
    assert stand.plex_geprueft


def test_a_missing_library_shows_up_as_a_problem(session, gateway, uebergaenge_an):
    stand = transition_build.status(session, "Alex", gateway.server)   # ohne Bibliothek

    assert stand.hat_fehler
    assert not {c.name: c for c in stand.checks}["Plex-Bibliothek"].ok


def test_the_panel_reports_progress_while_rendering(session, uebergaenge_an):
    db.set_transition_state(session, "Alex", "rendering", "Dienstag fertig", 2, 5)

    stand = transition_build.status(session, "Alex")

    assert stand.laeuft and stand.phase == "rendering"
    assert (stand.done, stand.total) == (2, 5)
    assert stand.message == "Dienstag fertig"


# ---------------------------------------------------------------------------
# Bedienung
# ---------------------------------------------------------------------------


def test_the_status_fragment_is_rendered(client, session, uebergaenge_an):
    db.set_transition_state(session, "Alex", "rendering", "läuft gerade", 1, 3)

    seite = client.get("/transitions/status?plex=0").text

    assert "wird gerendert" in seite
    assert "läuft gerade" in seite
    assert "every 4s" in seite            # holt sich den nächsten Stand selbst


def test_a_finished_run_stops_polling_and_looks_at_plex_once(client, session, uebergaenge_an):
    db.set_transition_state(session, "Alex", "ok", "3 Clips in der Playlist", 3, 3)

    seite = client.get("/transitions/status?plex=0").text

    assert "every 4s" not in seite                  # nichts läuft mehr
    assert "load delay:400ms" in seite              # einmal noch mit Plex nachsehen


def test_the_build_button_queues_a_run(client, session, uebergaenge_an):
    from app.scheduler import TRANSITION_JOB_PREFIX, get_scheduler

    antwort = client.post("/transitions/build")

    assert antwort.status_code == 200
    stand = db.get_or_create_user_state(session, "Alex")
    assert stand.transition_phase == "queued"
    assert get_scheduler().scheduler.get_job(f"{TRANSITION_JOB_PREFIX}Alex") is not None


def test_the_build_button_reports_a_missing_scheduler(client, session, uebergaenge_an):
    """Ohne Scheduler passiert nichts – das muss man sehen, nicht raten."""
    from app.scheduler import get_scheduler, set_scheduler

    vorhanden = get_scheduler()
    set_scheduler(None)
    try:
        antwort = client.post("/transitions/build")
    finally:
        set_scheduler(vorhanden)

    assert antwort.status_code == 200
    stand = db.get_or_create_user_state(session, "Alex")
    assert stand.transition_phase == "error"
    assert "Scheduler" in stand.transition_message


def test_the_build_button_refuses_a_profile_without_transitions(client, session, uebergaenge_an,
                                                                monkeypatch):
    from app import config

    monkeypatch.setenv("PTM_TRANSITION_USER", "Zeitreisende Ente")
    config.get_settings.cache_clear()

    client.post("/transitions/build")

    stand = db.get_or_create_user_state(session, "Alex")
    assert stand.transition_phase == "error"
    assert "ausgeschaltet" in stand.transition_message


# ---------------------------------------------------------------------------
# Protokoll
# ---------------------------------------------------------------------------


def test_the_log_buffer_keeps_the_youngest_lines():
    logbuffer.install()
    logbuffer.clear()
    log = logging.getLogger("app.transitions")

    for i in range(5):
        log.info("Zeile %s", i)
    log.error("So nicht")

    zeilen = logbuffer.lines()
    assert [z.message for z in zeilen][-2:] == ["Zeile 4", "So nicht"]
    assert [z.message for z in logbuffer.lines(problems_only=True)] == ["So nicht"]
    assert [z.message for z in logbuffer.lines(only=("nichts",))] == []
    assert logbuffer.lines()[-1].short_logger == "transitions"


def test_the_log_page_shows_recent_lines(client):
    logbuffer.clear()
    logging.getLogger("app.transitions").warning("Etwas ist schiefgegangen")

    seite = client.get("/logs").text

    assert "Etwas ist schiefgegangen" in seite
    assert "Protokoll" in seite


def test_the_log_buffer_does_not_grow_without_bound():
    logbuffer.install()
    logbuffer.clear()
    log = logging.getLogger("app.transitions")

    for i in range(logbuffer.CAPACITY + 50):
        log.info("Zeile %s", i)

    assert len(logbuffer.lines(limit=10_000)) == logbuffer.CAPACITY


# ---------------------------------------------------------------------------
# Webhooks: Plex und Tautulli
# ---------------------------------------------------------------------------


def test_tautulli_may_report_in_its_own_words(client, session):
    """Was eine Meldung wert ist, entscheidet der Agent in Tautulli.

    Deshalb wird hier nicht nach Ereignisnamen gefiltert – anders als beim
    Plex-Webhook, der für jeden Klick feuert.
    """
    db.set_period(session, "Alex", ERA_START, ERA_END)

    antwort = client.post(
        "/webhook/tautulli",
        json={"user": "Zeitreisende Ente", "action": "watched", "title": "Pilot"},
    )

    assert antwort.status_code == 200
    assert antwort.json()["status"] == "scheduled"


def test_tautulli_is_accepted_without_a_body(client):
    """Ein Agent ohne JSON-Rumpf soll trotzdem etwas auslösen."""
    antwort = client.post("/webhook/tautulli", content=b"")

    assert antwort.json()["status"] == "scheduled"

    kaputt = client.post("/webhook/tautulli", content=b"kein json")
    assert kaputt.json()["status"] == "scheduled"


def test_the_plex_webhook_still_ignores_irrelevant_events(client):
    """Der Plex-Weg bleibt streng – media.play soll nichts auslösen."""
    antwort = client.post("/webhook/plex", json={"event": "media.play"})

    assert antwort.json()["status"] == "ignored"


def test_both_webhooks_check_the_token(client, monkeypatch):
    from app import config

    monkeypatch.setenv("PTM_WEBHOOK_TOKEN", "geheim")
    config.get_settings.cache_clear()

    assert client.post("/webhook/tautulli", json={}).status_code == 401
    assert client.post("/webhook/plex", json={"event": "media.scrobble"}).status_code == 401

    assert client.post("/webhook/tautulli?token=geheim", json={}).status_code == 200
    # Kopfzeile geht auch – so steht das Geheimnis in keiner URL.
    mit_kopf = client.post("/webhook/tautulli", json={}, headers={"X-PTM-Token": "geheim"})
    assert mit_kopf.status_code == 200

    config.get_settings.cache_clear()


def test_the_footer_says_whether_a_webhook_ever_arrived(client):
    """Beim Einrichten die wichtigste Frage – ohne im Log zu suchen."""
    assert "noch keiner eingegangen" in client.get("/").text

    client.post("/webhook/tautulli", json={"user": "Zeitreisende Ente"})

    seite = client.get("/").text
    assert "Webhook: Tautulli" in seite
