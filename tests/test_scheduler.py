"""Tests für Polling-Job, Webhook-Entprellung und Sync-Runner."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config, db
from app.plex_client import set_gateway
from app.scheduler import POLL_JOB_ID, WEBHOOK_JOB_ID, SyncScheduler, run_sync_all


def _with_scheduler(settings_env: dict, body):
    """Scheduler in einem eigenen Event-Loop starten und wieder stoppen."""

    async def runner():
        scheduler = SyncScheduler()
        scheduler.start()
        try:
            return body(scheduler)
        finally:
            scheduler.shutdown()

    return asyncio.run(runner())


def test_poll_job_registered_for_positive_interval(monkeypatch):
    monkeypatch.setenv("PTM_POLL_INTERVAL_MINUTES", "15")
    config.get_settings.cache_clear()

    def check(scheduler: SyncScheduler):
        assert scheduler.scheduler.get_job(POLL_JOB_ID) is not None
        assert scheduler.next_poll_at is not None

    _with_scheduler({}, check)
    config.get_settings.cache_clear()


def test_polling_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PTM_POLL_INTERVAL_MINUTES", "0")
    config.get_settings.cache_clear()

    def check(scheduler: SyncScheduler):
        assert scheduler.scheduler.get_job(POLL_JOB_ID) is None
        assert scheduler.next_poll_at is None

    _with_scheduler({}, check)
    config.get_settings.cache_clear()


def test_webhook_syncs_are_debounced_into_one_job(monkeypatch):
    monkeypatch.setenv("PTM_WEBHOOK_DEBOUNCE_SECONDS", "30")
    config.get_settings.cache_clear()

    def check(scheduler: SyncScheduler):
        first = scheduler.request_webhook_sync()
        second = scheduler.request_webhook_sync()

        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id == WEBHOOK_JOB_ID]
        assert len(jobs) == 1  # zweites Event ersetzt das erste
        assert second >= first

    _with_scheduler({}, check)
    config.get_settings.cache_clear()


def test_run_sync_all_uses_global_gateway(session, gateway):
    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    set_gateway(gateway)
    try:
        run_sync_all("poll")
    finally:
        set_gateway(None)

    assert gateway.server.playlists()[0].title == "Friday - 22.02.1985 - Time Machine"
    journeys = db.list_journeys(session, "Alex")
    assert journeys and journeys[0].trigger == "poll"


# ---------------------------------------------------------------------------
# Zeitpunkt des ersten Laufs
# ---------------------------------------------------------------------------


def _first_run_delay(tz: str) -> timedelta:
    """Abstand zwischen jetzt und dem ersten geplanten Poll – in echter Zeit."""
    alt = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    config.get_settings.cache_clear()
    try:
        holder = {}

        def check(scheduler: SyncScheduler):
            job = scheduler.scheduler.get_job(POLL_JOB_ID)
            holder["delta"] = job.next_run_time - datetime.now(timezone.utc)

        _with_scheduler({}, check)
        return holder["delta"]
    finally:
        if alt is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = alt
        time.tzset()
        config.get_settings.cache_clear()


def test_first_poll_runs_shortly_after_start(monkeypatch):
    """Nach dem Start soll zügig einmal nachgezogen werden."""
    monkeypatch.setenv("PTM_POLL_INTERVAL_MINUTES", "30")

    delta = _first_run_delay("UTC")

    assert timedelta(seconds=0) < delta <= timedelta(minutes=2)


@pytest.mark.parametrize("tz", ["UTC", "Europe/Berlin", "America/Los_Angeles"])
def test_first_poll_ignores_the_local_timezone(monkeypatch, tz):
    """Die lokale Zeitzone darf den Start nicht verschieben.

    Vorher wurde eine naive lokale Zeit an den auf UTC laufenden Scheduler
    gegeben – in Berlin verzögerte das den ersten Lauf um zwei Stunden.
    """
    monkeypatch.setenv("PTM_POLL_INTERVAL_MINUTES", "30")

    delta = _first_run_delay(tz)

    assert timedelta(seconds=0) < delta <= timedelta(minutes=2), (
        f"Erster Lauf in {tz} erst in {delta}"
    )


def test_webhook_sync_is_scheduled_in_real_time(monkeypatch):
    """Auch die Webhook-Verzögerung darf nicht an der Zeitzone hängen."""
    monkeypatch.setenv("PTM_WEBHOOK_DEBOUNCE_SECONDS", "20")
    alt = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    config.get_settings.cache_clear()
    try:
        holder = {}

        def check(scheduler: SyncScheduler):
            scheduler.request_webhook_sync()
            job = scheduler.scheduler.get_job(WEBHOOK_JOB_ID)
            holder["delta"] = job.next_run_time - datetime.now(timezone.utc)

        _with_scheduler({}, check)
        assert timedelta(seconds=0) < holder["delta"] <= timedelta(seconds=25)
    finally:
        if alt is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = alt
        time.tzset()
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Gesehenes verschwindet – über genau den Weg, den der Scheduler nimmt
# ---------------------------------------------------------------------------


def test_poll_removes_watched_movie_from_the_time_machine_playlist(session, gateway, plex_data):
    from app.plex_client import set_gateway

    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    set_gateway(gateway)
    try:
        run_sync_all("poll")
        playlist = gateway.server.playlists()[0]
        assert "Brazil" in [i.title for i in playlist.items()]

        plex_data["movies"][1].viewCount = 1  # Brazil gesehen
        run_sync_all("poll")
    finally:
        set_gateway(None)

    playlist = gateway.server.playlists()[0]
    assert "Brazil" not in [i.title for i in playlist.items()]
    assert db.get_or_create_user_state(session, "Alex").last_item_count == 4


def test_poll_removes_watched_episode_from_the_almanach_playlist(session, gateway, plex_data):
    from app.plex_client import set_gateway

    almanach = db.create_almanach(session, "Alex", "Knight Rider")
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    set_gateway(gateway)
    try:
        run_sync_all("poll")
        assert len(gateway.server.playlists()[0].items()) == 2

        plex_data["episodes"][0].viewCount = 1  # Pilot gesehen
        run_sync_all("poll")
    finally:
        set_gateway(None)

    playlist = gateway.server.playlists()[0]
    assert [i.title for i in playlist.items()] == ["Folge 2"]


def test_poll_removes_the_playlist_once_everything_is_watched(session, gateway, plex_data):
    from app.plex_client import set_gateway

    almanach = db.create_almanach(session, "Alex", "Knight Rider")
    db.add_to_almanach(session, almanach, "100", "show", "Knight Rider")

    set_gateway(gateway)
    try:
        run_sync_all("poll")
        assert gateway.server.playlists()

        for episode in plex_data["episodes"][:2]:
            episode.viewCount = 1
        run_sync_all("poll")
    finally:
        set_gateway(None)

    assert gateway.server.playlists() == []


def test_poll_logs_every_run_in_the_logbook(session, gateway):
    from app.plex_client import set_gateway

    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    set_gateway(gateway)
    try:
        run_sync_all("poll")
    finally:
        set_gateway(None)

    journeys = db.list_journeys(session, "Alex")
    assert journeys and journeys[0].trigger == "poll"
