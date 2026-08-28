"""APScheduler-Setup: periodisches Polling + entprellte Webhook-Syncs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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
TRANSITION_JOB_PREFIX = "ptm-transitions-"

#: Kurz nach dem Start einmal nachziehen – holt nach, was während der Auszeit
#: gesehen wurde, und macht sichtbar, dass das Polling läuft.
STARTUP_DELAY_SECONDS = 60


def run_sync_all(trigger: str) -> None:
    """Blockierender Sync aller Nutzer – läuft im Worker-Thread.

    Zieht beide Playlist-Arten nach: die Zeitreise-Playlist jedes Nutzers mit
    gesetztem Zeitraum und jede Almanach-Playlist.
    """
    log.info("[%s] Aktualisierung gestartet", trigger)
    with Session(db.get_engine(), expire_on_commit=False) as session:
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
    log.info(
        "[%s] Aktualisierung fertig: %s Playlists, davon %s mit Fehler",
        trigger,
        len(results),
        sum(1 for r in results if not r.ok),
    )


def run_transition_build(user_id: str) -> None:
    """Clips erzeugen, Plex einlesen lassen, Playlist neu bauen."""
    from app import transition_build
    from app.plex_client import get_gateway
    from app.sync_engine import sync_user

    with Session(db.get_engine(), expire_on_commit=False) as session:
        state = db.get_or_create_user_state(session, user_id)
        if not state.has_period:
            return
        periode = (state.current_date_start, state.current_date_end)

        from app.sync_engine import apply_blacklist, collect_items

        gateway = get_gateway()
        try:
            server = gateway.connect_as(user_id)
            roh = collect_items(gateway, server, *periode)
            items, _ = apply_blacklist(roh, db.blacklist_keys(session, user_id))
            session.commit()
            titel = transition_build.build_clips(session, user_id, items, periode, gateway)
            if titel:
                transition_build.rescan_library(server)
                transition_build.find_clips(server, titel, warten=True)
        except Exception as exc:
            log.warning("Übergänge für %s fehlgeschlagen: %s", user_id, exc)
            return

        if titel:
            log.info("%s Übergänge fertig – Playlist wird neu gebaut", len(titel))
            sync_user(session, user_id, trigger="transitions")


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
            # Bewusst zeitzonenbewusst: der Scheduler rechnet in UTC. Eine naive
            # lokale Zeit würde als UTC gelesen und den ersten Lauf um den
            # Zeitzonen-Versatz verschieben (in Berlin um zwei Stunden).
            first_run = datetime.now(timezone.utc) + timedelta(
                seconds=min(STARTUP_DELAY_SECONDS, interval * 60)
            )
            self.scheduler.add_job(
                self._poll,
                "interval",
                minutes=interval,
                id=POLL_JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                next_run_time=first_run,
            )
            log.info(
                "Polling aktiv: alle %s Minuten, erster Lauf %s UTC",
                interval,
                first_run.strftime("%H:%M:%S"),
            )
        else:
            log.info("Polling deaktiviert (PTM_POLL_INTERVAL_MINUTES=0)")
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # -- Jobs --------------------------------------------------------------

    async def _poll(self) -> None:
        self.last_poll_at = datetime.now(timezone.utc)
        await asyncio.to_thread(run_sync_all, "poll")

    async def _webhook(self) -> None:
        await asyncio.to_thread(run_sync_all, "webhook")

    def request_transition_build(self, user_id: str) -> None:
        """Übergangsclips für einen Nutzer im Hintergrund erzeugen.

        Das Rendern dauert je Tag rund eine halbe Minute; deshalb passiert es
        nie im Web-Aufruf, sondern hier – und danach wird die Playlist noch
        einmal gebaut, damit die Clips darin landen.
        """
        auftrag = f"{TRANSITION_JOB_PREFIX}{user_id}"
        if self.scheduler.get_job(auftrag) is not None:
            return  # läuft schon
        self.scheduler.add_job(
            self._build_transitions,
            "date",
            args=[user_id],
            run_date=datetime.now(timezone.utc) + timedelta(seconds=2),
            id=auftrag,
            replace_existing=True,
            misfire_grace_time=300,
        )
        log.info("Übergänge für %s werden im Hintergrund erzeugt", user_id)

    async def _build_transitions(self, user_id: str) -> None:
        await asyncio.to_thread(run_transition_build, user_id)

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
