"""APScheduler-Setup: periodisches Polling + entprellte Webhook-Syncs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session

from app import db
from app.config import Settings, get_settings
from app.almanach import sync_all_almanachs
from app.sync_engine import sync_all_users

log = logging.getLogger(__name__)

POLL_JOB_ID = "ptm-poll"
WEBHOOK_JOB_ID = "ptm-webhook"


def run_sync_all(trigger: str) -> None:
    """Blockierender Sync aller Nutzer – läuft im Worker-Thread.

    Zieht beide Playlist-Arten nach: die Zeitreise-Playlist jedes Nutzers mit
    gesetztem Zeitraum und die Almanach-Playlist jedes Nutzers mit Auswahl.
    """
    with Session(db.get_engine()) as session:
        results = list(sync_all_users(session, trigger=trigger))
        results += sync_all_almanachs(session, trigger=trigger)
    for result in results:
        if result.ok:
            log.info(
                "[%s] %s: %s Items -> %s",
                trigger,
                result.user_id,
                result.item_count,
                result.playlist_name,
            )
        else:
            log.warning("[%s] %s fehlgeschlagen: %s", trigger, result.user_id, result.error)


class SyncScheduler:
    """Kapselt den APScheduler, damit die App ihn sauber starten/stoppen kann."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.last_poll_at: Optional[datetime] = None

    # -- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        interval = self.settings.poll_interval_minutes
        if interval > 0:
            self.scheduler.add_job(
                self._poll,
                "interval",
                minutes=interval,
                id=POLL_JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(minutes=interval),
            )
            log.info("Polling aktiv: alle %s Minuten", interval)
        else:
            log.info("Polling deaktiviert (PTM_POLL_INTERVAL_MINUTES=0)")
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # -- Jobs --------------------------------------------------------------

    async def _poll(self) -> None:
        self.last_poll_at = datetime.now()
        await asyncio.to_thread(run_sync_all, "poll")

    async def _webhook(self) -> None:
        await asyncio.to_thread(run_sync_all, "webhook")

    def request_webhook_sync(self) -> datetime:
        """Sync nach Webhook-Event anstossen – mehrere Events werden entprellt."""
        delay = max(self.settings.webhook_debounce_seconds, 1)
        run_at = datetime.now(self.scheduler.timezone) + timedelta(seconds=delay)
        self.scheduler.add_job(
            self._webhook,
            "date",
            run_date=run_at,
            id=WEBHOOK_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        return run_at

    # -- Status für die UI ------------------------------------------------

    @property
    def next_poll_at(self) -> Optional[datetime]:
        job = self.scheduler.get_job(POLL_JOB_ID) if self.scheduler.running else None
        return getattr(job, "next_run_time", None) if job else None


_scheduler: Optional[SyncScheduler] = None


def get_scheduler() -> Optional[SyncScheduler]:
    return _scheduler


def set_scheduler(scheduler: Optional[SyncScheduler]) -> None:
    global _scheduler
    _scheduler = scheduler
