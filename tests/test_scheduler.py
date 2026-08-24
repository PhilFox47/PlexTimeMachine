"""Tests für Polling-Job, Webhook-Entprellung und Sync-Runner."""

from __future__ import annotations

import asyncio
from datetime import date

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

    assert gateway.server.playlists()[0].title == "Plex Time Machine – Alex"
    journeys = db.list_journeys(session, "Alex")
    assert journeys and journeys[0].trigger == "poll"
