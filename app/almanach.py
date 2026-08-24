"""Almanach: gezielt gewählte Serien und Filme als eigene Release-Order-Playlist.

Anders als die Zeitreise arbeitet der Almanach nicht über einen Zeitraum,
sondern über eine bewusst zusammengestellte Liste. Aus jeder ausgewählten Serie
kommen alle ungesehenen Episoden dazu, aus jedem Film der Film selbst – und
alles zusammen wird streng nach Erscheinungsdatum sortiert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from plexapi.exceptions import NotFound
from sqlmodel import Session

from app import db
from app.config import get_settings
from app.plex_client import PlexGateway, PlexUnavailable, get_gateway
from app.sync_engine import (
    PreviewItem,
    PreviewResult,
    SyncResult,
    apply_playlist,
    to_preview_item,
)

log = logging.getLogger(__name__)

#: Obergrenze je Bibliothek, damit eine unscharfe Suche die UI nicht flutet.
SEARCH_LIMIT = 25


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    """Ein Treffer der Titelsuche – Serie oder Film."""

    rating_key: str
    media_type: str  # "movie" | "show"
    title: str
    year: Optional[int] = None
    thumb: str = ""
    episode_count: Optional[int] = None
    already_added: bool = False

    @property
    def is_show(self) -> bool:
        return self.media_type == "show"


@dataclass
class SearchResult:
    query: str = ""
    hits: list[SearchHit] = field(default_factory=list)
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_hit(obj: Any) -> Optional[SearchHit]:
    rating_key = getattr(obj, "ratingKey", None)
    media_type = getattr(obj, "type", "")
    if rating_key is None or media_type not in {"movie", "show"}:
        return None
    return SearchHit(
        rating_key=str(rating_key),
        media_type=media_type,
        title=getattr(obj, "title", "") or "",
        year=_as_int(getattr(obj, "year", None)),
        thumb=getattr(obj, "thumb", "") or "",
        episode_count=_as_int(getattr(obj, "leafCount", None))
        if media_type == "show"
        else None,
    )


def search_titles(
    session: Session,
    user_id: str,
    query: str,
    gateway: Optional[PlexGateway] = None,
    limit: int = SEARCH_LIMIT,
) -> SearchResult:
    """Serien und Filme suchen, deren Titel ``query`` enthält."""
    query = (query or "").strip()
    if not query:
        return SearchResult(query=query)

    gateway = gateway or get_gateway()
    try:
        server = gateway.connect_as(user_id)
        movies = gateway.movie_section(server).search(
            title=query, libtype="movie", maxresults=limit
        )
        shows = gateway.tv_section(server).search(
            title=query, libtype="show", maxresults=limit
        )
    except PlexUnavailable as exc:
        return SearchResult(query=query, error=str(exc))
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Almanach-Suche fehlgeschlagen")
        return SearchResult(query=query, error=f"Unerwarteter Fehler: {exc}")

    chosen = db.almanach_keys(session, user_id)
    hits: list[SearchHit] = []
    for obj in list(shows) + list(movies):
        hit = _to_hit(obj)
        if hit is None:
            continue
        hit.already_added = hit.rating_key in chosen
        hits.append(hit)

    hits.sort(key=lambda h: (h.title.lower(), h.year or 0))
    truncated = len(hits) > limit
    return SearchResult(query=query, hits=hits[:limit], truncated=truncated)


# ---------------------------------------------------------------------------
# Zusammenstellen
# ---------------------------------------------------------------------------


def collect_almanach_items(
    server: Any, entries: Iterable[db.AlmanachEntry]
) -> tuple[list[PreviewItem], list[str]]:
    """Alle ungesehenen Titel der Auswahl in Release-Order.

    Rückgabe: (Items, Titel nicht mehr auffindbarer Einträge).
    """
    items: list[PreviewItem] = []
    missing: list[str] = []

    for entry in entries:
        try:
            obj = server.fetchItem(int(entry.plex_rating_key))
        except (NotFound, ValueError):
            missing.append(entry.title or entry.plex_rating_key)
            continue

        if getattr(obj, "type", "") == "show":
            candidates: Sequence[Any] = obj.unwatched()
        elif getattr(obj, "viewCount", 0):
            candidates = []  # Film bereits gesehen
        else:
            candidates = [obj]

        for candidate in candidates:
            item = to_preview_item(candidate)
            if item is not None:
                items.append(item)

    # Ein Titel kann über mehrere Einträge hereinkommen – nur einmal aufnehmen.
    unique: dict[str, PreviewItem] = {}
    for item in items:
        unique.setdefault(item.rating_key, item)

    ordered = sorted(unique.values(), key=lambda i: i.sort_key)
    return ordered, missing


def build_preview(
    session: Session,
    user_id: str,
    gateway: Optional[PlexGateway] = None,
    limit: Optional[int] = None,
) -> PreviewResult:
    """Vorschau der Almanach-Playlist – ohne etwas in Plex zu verändern."""
    entries = db.list_almanach(session, user_id)
    if not entries:
        return PreviewResult()

    gateway = gateway or get_gateway()
    try:
        server = gateway.connect_as(user_id)
        items, missing = collect_almanach_items(server, entries)
    except PlexUnavailable as exc:
        return PreviewResult(error=str(exc))
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Almanach-Vorschau fehlgeschlagen")
        return PreviewResult(error=f"Unerwarteter Fehler: {exc}")

    if limit is None:
        limit = get_settings().preview_limit
    truncated = bool(limit) and len(items) > limit

    return PreviewResult(
        items=items[:limit] if truncated else items,
        total=len(items),
        blacklisted=len(missing),  # hier: nicht mehr auffindbare Einträge
        movies=sum(1 for i in items if not i.is_episode),
        episodes=sum(1 for i in items if i.is_episode),
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Playlist schreiben
# ---------------------------------------------------------------------------


def sync_almanach(
    session: Session,
    user_id: str,
    trigger: str = "manual",
    gateway: Optional[PlexGateway] = None,
) -> SyncResult:
    """Almanach-Playlist des Nutzers neu aufbauen."""
    gateway = gateway or get_gateway()
    state = db.get_or_create_almanach_state(session, user_id)
    playlist_name = state.target_playlist_name or get_settings().almanach_playlist_name_for(
        user_id
    )
    entries = db.list_almanach(session, user_id)

    if not entries:
        return SyncResult(
            user_id=user_id,
            playlist_name=playlist_name,
            trigger=trigger,
            changed=False,
            error="Der Almanach ist leer – bitte zuerst Serien oder Filme aufnehmen.",
        )

    try:
        server = gateway.connect_as(user_id)
        items, missing = collect_almanach_items(server, entries)
        exists = apply_playlist(server, playlist_name, [i.plex_object for i in items])
    except PlexUnavailable as exc:
        return SyncResult(
            user_id=user_id, playlist_name=playlist_name, trigger=trigger, error=str(exc)
        )
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Almanach-Sync für %s fehlgeschlagen", user_id)
        return SyncResult(
            user_id=user_id,
            playlist_name=playlist_name,
            trigger=trigger,
            error=f"Unerwarteter Fehler: {exc}",
        )

    state.last_synced_at = db.utcnow()
    state.last_item_count = len(items)
    state.target_playlist_name = playlist_name
    session.add(state)
    session.commit()

    note = f"{len(missing)} Einträge nicht mehr in der Bibliothek" if missing else ""
    db.log_journey(
        session, user_id, None, None, len(items), trigger=trigger, note=note, kind="almanach"
    )

    if exists:
        message = f"{len(items)} ungesehene Titel in »{playlist_name}« gespeichert."
    else:
        message = (
            f"Alles gesehen – »{playlist_name}« wurde geleert bzw. entfernt."
        )
    if missing:
        message += f" ({', '.join(missing)} nicht mehr in der Bibliothek)"

    return SyncResult(
        user_id=user_id,
        playlist_name=playlist_name,
        item_count=len(items),
        trigger=trigger,
        message=message,
    )


def sync_all_almanachs(
    session: Session, trigger: str = "poll", gateway: Optional[PlexGateway] = None
) -> list[SyncResult]:
    """Alle Nutzer mit gefülltem Almanach nachziehen (Scheduler/Webhook)."""
    return [
        sync_almanach(session, user_id, trigger=trigger, gateway=gateway)
        for user_id in db.users_with_almanach(session)
    ]
