"""Übergangsclips erzeugen, pflegen und in die Playlist einfädeln.

Aufgabenteilung: ``transitions.py`` malt und kodiert, dieses Modul weiß, für
welche Tage ein Clip nötig ist, holt die Poster aus Plex, räumt alte Dateien
weg und findet die Clips in der Plex-Bibliothek wieder.

Die Clips gehören zu einem Zeitraum. Wechselt der Nutzer die Woche, werden alle
alten verworfen und neue erzeugt – innerhalb einer Woche bleiben sie stehen,
auch wenn Titel weggesehen werden.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import requests
from sqlmodel import Session

from app import db, transitions
from app.config import get_settings
from app.formatting import weekday_long
from app.plex_client import PlexGateway, PlexUnavailable, get_gateway
from app.sync_engine import PreviewItem
from app.transitions import ClipItem, ClipSpec

log = logging.getLogger(__name__)

#: So lange wird nach dem Einlesen auf den neuen Plex-Eintrag gewartet.
SCAN_TIMEOUT_SECONDS = 90
SCAN_POLL_SECONDS = 3


def transition_dir() -> Path:
    pfad = Path(get_settings().transition_dir)
    pfad.mkdir(parents=True, exist_ok=True)
    return pfad


# ---------------------------------------------------------------------------
# Welche Tage bekommen einen Clip?
# ---------------------------------------------------------------------------


def group_by_day(items: Sequence[PreviewItem]) -> list[tuple[date, list[PreviewItem]]]:
    """Titel nach Erscheinungstag bündeln – Reihenfolge bleibt chronologisch.

    Tage ohne Titel entstehen dabei gar nicht erst und werden so übersprungen.
    """
    tage: dict[date, list[PreviewItem]] = {}
    for item in items:
        if item.air_date is None:
            continue
        tage.setdefault(item.air_date, []).append(item)
    return sorted(tage.items())


def clip_title(user_id: str, day: date) -> str:
    """Titel des Clips in Plex – daraus wird auch der Dateiname."""
    return f"Time Machine - {weekday_long(day)} {day.strftime('%d.%m.%Y')} - {user_id}"


def clip_file_name(user_id: str, day: date) -> str:
    sauber = re.sub(r"[^\w\s.\-]", "_", clip_title(user_id, day), flags=re.UNICODE)
    return f"{sauber}.mp4"


# ---------------------------------------------------------------------------
# Poster besorgen
# ---------------------------------------------------------------------------


def fetch_image(server: Any, pfad: str, timeout: int = 15) -> Optional[bytes]:
    """Ein Bild aus Plex laden – None, wenn es nicht klappt."""
    if not pfad:
        return None
    try:
        antwort = requests.get(server.url(pfad, includeToken=True), timeout=timeout)
        if antwort.status_code == 200 and antwort.content:
            return antwort.content
    except requests.RequestException as exc:
        log.debug("Poster '%s' nicht ladbar: %s", pfad, exc)
    return None


def to_clip_item(server: Any, item: PreviewItem) -> ClipItem:
    """Vorschau-Objekt in einen Kacheleintrag übersetzen.

    Für Episoden wird bewusst das Staffelposter genommen: es zeigt die Staffel
    ohnehin an und kommt in Plex sonst kaum zur Geltung.
    """
    if item.is_episode:
        poster = fetch_image(server, item.parent_thumb) or fetch_image(server, item.thumb)
        return ClipItem(
            kind="episode",
            show=item.series_title,
            title=item.title,
            season=item.season,
            episode=item.episode,
            poster=poster,
        )
    return ClipItem(
        kind="movie",
        show=item.title,
        title=item.title,
        year=item.year,
        poster=fetch_image(server, item.thumb),
    )


def spec_for_day(
    server: Any,
    vortag: date,
    tag: date,
    tages_items: Sequence[PreviewItem],
) -> ClipSpec:
    return ClipSpec(
        prev_weekday=weekday_long(vortag),
        prev_date=vortag.strftime("%d.%m.%Y"),
        weekday=weekday_long(tag),
        date=tag.strftime("%d.%m.%Y"),
        items=[to_clip_item(server, i) for i in tages_items],
    )


# ---------------------------------------------------------------------------
# Erzeugen und aufräumen
# ---------------------------------------------------------------------------


def needs_rebuild(
    session: Session, user_id: str, period: tuple[Optional[date], Optional[date]]
) -> bool:
    """Neu erzeugen, sobald der Zeitraum ein anderer ist (oder nichts da ist)."""
    vorhanden = db.list_transition_clips(session, user_id)
    if not vorhanden:
        return True
    return any(
        (clip.period_start, clip.period_end) != period for clip in vorhanden
    )


def discard_clips(session: Session, user_id: str) -> int:
    """Dateien und Einträge eines Nutzers entfernen."""
    ordner = transition_dir()
    entfernt = 0
    for name in db.drop_transition_clips(session, user_id):
        datei = ordner / name
        if datei.exists():
            datei.unlink()
            entfernt += 1
    return entfernt


def build_clips(
    session: Session,
    user_id: str,
    items: Sequence[PreviewItem],
    period: tuple[Optional[date], Optional[date]],
    gateway: Optional[PlexGateway] = None,
) -> list[str]:
    """Alle Clips für den aktuellen Zeitraum erzeugen. Gibt die Titel zurück.

    Läuft bewusst als Ganzes: alte weg, neue hin. Das ist einfacher zu
    durchschauen als eine Teilaktualisierung und passt zum wochenweisen Rhythmus.
    """
    einstellungen = get_settings()
    gateway = gateway or get_gateway()
    server = gateway.connect_as(user_id)

    discard_clips(session, user_id)

    tage = group_by_day(items)[: max(1, einstellungen.transition_max_clips)]
    if not tage:
        return []

    ordner = transition_dir()
    titel: list[str] = []
    vortag = period[0] or tage[0][0]

    for tag, tages_items in tage:
        spec = spec_for_day(server, vortag, tag, tages_items)
        name = clip_file_name(user_id, tag)
        try:
            transitions.render_clip(
                spec,
                ordner / name,
                height=einstellungen.transition_height,
                ffmpeg=einstellungen.ffmpeg_binary,
            )
        except transitions.RenderError as exc:
            log.error("Übergang für %s (%s) nicht erzeugt: %s", user_id, tag, exc)
            break
        db.add_transition_clip(
            session, user_id, tag, period, name, clip_title(user_id, tag), len(tages_items)
        )
        titel.append(clip_title(user_id, tag))
        log.info("Übergang erzeugt: %s (%s Titel)", name, len(tages_items))
        vortag = tag

    return titel


# ---------------------------------------------------------------------------
# In Plex wiederfinden und einfädeln
# ---------------------------------------------------------------------------


def _library(server: Any):
    name = get_settings().transition_library
    if not name:
        raise PlexUnavailable("Keine Bibliothek für Übergänge konfiguriert.")
    try:
        return server.library.section(name)
    except Exception as exc:
        raise PlexUnavailable(
            f"Bibliothek '{name}' für die Übergänge nicht gefunden: {exc}"
        ) from exc


def rescan_library(server: Any) -> None:
    """Plex bitten, den Ordner neu einzulesen."""
    try:
        _library(server).update()
    except PlexUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - Plex kann ablehnen
        log.warning("Bibliothek konnte nicht eingelesen werden: %s", exc)


def find_clips(server: Any, titel: Iterable[str], warten: bool = False) -> dict[str, Any]:
    """Die Clips in der Plex-Bibliothek suchen; optional auf den Scan warten."""
    gesucht = list(titel)
    if not gesucht:
        return {}
    section = _library(server)
    frist = time.monotonic() + (SCAN_TIMEOUT_SECONDS if warten else 0)

    while True:
        gefunden = {}
        for eintrag in section.search():
            if eintrag.title in gesucht:
                gefunden[eintrag.title] = eintrag
        if len(gefunden) == len(gesucht) or time.monotonic() >= frist:
            if len(gefunden) < len(gesucht):
                log.warning(
                    "%s von %s Übergängen noch nicht in Plex sichtbar – kommen beim "
                    "nächsten Lauf dazu.", len(gesucht) - len(gefunden), len(gesucht)
                )
            return gefunden
        time.sleep(SCAN_POLL_SECONDS)


def interleave(
    items: Sequence[PreviewItem], clips_by_day: dict[date, Any]
) -> list[Any]:
    """Vor jeden Tag seinen Übergang setzen."""
    ergebnis: list[Any] = []
    for tag, tages_items in group_by_day(items):
        clip = clips_by_day.get(tag)
        if clip is not None:
            ergebnis.append(clip)
        ergebnis.extend(i.plex_object for i in tages_items)
    # Titel ohne Datum hängen hinten an (kommt praktisch nicht vor)
    ergebnis.extend(i.plex_object for i in items if i.air_date is None)
    return ergebnis


def clips_for_playlist(
    session: Session, user_id: str, server: Any, warten: bool = False
) -> dict[date, Any]:
    """Die vorhandenen Clips eines Nutzers als {Tag: Plex-Eintrag}."""
    vorhanden = db.list_transition_clips(session, user_id)
    if not vorhanden:
        return {}
    try:
        gefunden = find_clips(server, [c.title for c in vorhanden], warten=warten)
    except PlexUnavailable as exc:
        log.warning("Übergänge nicht auffindbar: %s", exc)
        return {}
    return {c.day: gefunden[c.title] for c in vorhanden if c.title in gefunden}
