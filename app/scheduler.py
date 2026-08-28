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
PUBLISH_JOB_PREFIX = "ptm-transitions-publish-"

#: So oft wird versucht, die frisch gerenderten Clips in Plex wiederzufinden,
#: bevor die Playlist auch ohne die fehlenden gebaut wird.
MAX_PUBLISH_ATTEMPTS = 3

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
    """Clips erzeugen – mehr nicht.

    Das Einlesen in Plex passiert bewusst erst später (``run_transition_publish``):
    frisch geschriebene Dateien meldet Plex sonst gern als unvollständig zurück.
    """
    from app import transition_build
    from app.plex_client import get_gateway
    from app.sync_engine import apply_blacklist, collect_items

    titel: list[str] = []
    with Session(db.get_engine(), expire_on_commit=False) as session:
        state = db.get_or_create_user_state(session, user_id)
        if not state.has_period:
            return
        periode = (state.current_date_start, state.current_date_end)

        gateway = get_gateway()
        try:
            server = gateway.connect_as(user_id)
            roh = collect_items(gateway, server, *periode, db.all_slots(session))
            items, _ = apply_blacklist(roh, db.blacklist_keys(session, user_id))
            tage = len(transition_build.group_by_day(items))
            geplant = min(tage, max(1, get_settings().transition_max_clips))
            db.set_transition_state(session, user_id, "rendering",
                                    f"{geplant} Clips werden erzeugt", 0, geplant)
            log.info("Übergänge für %s werden gerendert (%s Clips) …", user_id, geplant)
            titel = transition_build.build_clips(session, user_id, items, periode, gateway)
        except Exception as exc:
            log.exception("Übergänge für %s fehlgeschlagen: %s", user_id, exc)
            db.set_transition_state(session, user_id, "error", str(exc)[:300])
            return

        if not titel:
            # Ein abgebrochener Rendervorgang hat den Grund schon hinterlegt –
            # der darf nicht mit "nichts zu tun" überschrieben werden.
            stand = db.get_or_create_user_state(session, user_id)
            if stand.transition_phase != "error":
                db.set_transition_state(session, user_id, "ok", "Nichts zu erzeugen")
            log.info("Für %s gab es nichts zu rendern", user_id)
            return

    if not titel:
        return

    verzoegerung = get_settings().transition_scan_delay_seconds
    log.info(
        "%s Übergänge für %s gerendert – Plex wird in %s Sekunden eingelesen",
        len(titel),
        user_id,
        verzoegerung,
    )
    with Session(db.get_engine(), expire_on_commit=False) as session:
        db.set_transition_state(
            session, user_id, "waiting",
            f"warte {verzoegerung} s, dann liest Plex den Ordner ein",
            len(titel), len(titel),
        )
    scheduler = get_scheduler()
    if scheduler is None:  # ohne laufenden Scheduler (z. B. im Test) direkt weiter
        run_transition_publish(user_id)
        return
    scheduler.request_transition_publish(user_id, delay=verzoegerung)


def run_transition_publish(user_id: str, attempt: int = 1) -> None:
    """Bibliothek einlesen, auf die Clips warten, Playlist neu bauen.

    Taucht noch nicht alles auf, wird es später erneut versucht – erst beim
    letzten Versuch wird die Playlist auch ohne die fehlenden Clips gebaut.
    """
    from app import transition_build
    from app.plex_client import get_gateway
    from app.sync_engine import sync_user

    with Session(db.get_engine(), expire_on_commit=False) as session:
        state = db.get_or_create_user_state(session, user_id)
        if not state.has_period:
            return
        titel = [clip.title for clip in db.list_transition_clips(session, user_id)]
        if not titel:
            return
        session.commit()

        try:
            server = get_gateway().connect_as(user_id)
            transition_build.rescan_library(server)
            gefunden = transition_build.find_clips(server, titel, warten=True)
        except Exception as exc:
            log.warning("Übergänge für %s nicht auffindbar: %s", user_id, exc)
            db.set_transition_state(session, user_id, "error", str(exc)[:300],
                                    0, len(titel))
            gefunden = {}

        fehlend = [name for name in titel if name not in gefunden]
        if fehlend and attempt < MAX_PUBLISH_ATTEMPTS:
            scheduler = get_scheduler()
            if scheduler is not None:
                verzoegerung = get_settings().transition_scan_delay_seconds
                log.info(
                    "%s von %s Übergängen fehlen in Plex – Versuch %s in %s Sekunden",
                    len(fehlend),
                    len(titel),
                    attempt + 1,
                    verzoegerung,
                )
                db.set_transition_state(
                    session, user_id, "waiting",
                    f"{len(fehlend)} Clips noch nicht in Plex sichtbar – "
                    f"Versuch {attempt + 1} von {MAX_PUBLISH_ATTEMPTS}",
                    len(gefunden), len(titel),
                )
                scheduler.request_transition_publish(
                    user_id, attempt=attempt + 1, delay=verzoegerung
                )
                return

        log.info(
            "Playlist von %s wird mit %s von %s Übergängen neu gebaut",
            user_id,
            len(gefunden),
            len(titel),
        )
        if fehlend:
            db.set_transition_state(
                session, user_id, "error",
                f"{len(fehlend)} von {len(titel)} Clips bleiben in Plex unsichtbar – "
                f"stimmt die Bibliothek »{get_settings().transition_library}«?",
                len(gefunden), len(titel),
            )
        else:
            db.set_transition_state(session, user_id, "ok",
                                    f"{len(titel)} Clips in der Playlist",
                                    len(titel), len(titel))
        sync_user(session, user_id, trigger="transitions")


class SyncScheduler:
    """Kapselt den APScheduler, damit die App ihn sauber starten/stoppen kann."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.last_poll_at: Optional[datetime] = None
        #: Wann zuletzt ein Webhook kam und woher – die Oberfläche zeigt es an,
        #: damit man beim Einrichten nicht raten muss, ob er ankommt.
        self.last_webhook_at: Optional[datetime] = None
        self.last_webhook_source: str = ""

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

    def request_transition_publish(
        self, user_id: str, attempt: int = 1, delay: Optional[int] = None
    ) -> None:
        """Nach der Wartezeit Plex einlesen lassen und die Playlist neu bauen.

        Die Pause gibt Plex Zeit, die frisch geschriebenen Dateien überhaupt
        als fertig zu erkennen.
        """
        if delay is None:
            delay = self.settings.transition_scan_delay_seconds
        self.scheduler.add_job(
            self._publish_transitions,
            "date",
            args=[user_id, attempt],
            run_date=datetime.now(timezone.utc) + timedelta(seconds=max(0, delay)),
            id=f"{PUBLISH_JOB_PREFIX}{user_id}",
            replace_existing=True,
            misfire_grace_time=600,
        )

    async def _publish_transitions(self, user_id: str, attempt: int) -> None:
        await asyncio.to_thread(run_transition_publish, user_id, attempt)

    def request_webhook_sync(self, source: str = "Plex") -> datetime:
        """Sync nach Webhook-Event anstossen – mehrere Events werden entprellt."""
        self.last_webhook_at = datetime.now(timezone.utc)
        self.last_webhook_source = source
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
