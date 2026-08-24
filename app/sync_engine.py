"""Sync-Engine: Suche, Blacklist-Filter, Merge/Sort und Playlist-Pflege."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from sqlmodel import Session

from app import db
from app.config import get_settings
from app.formatting import format_date
from app.plex_client import PlexGateway, PlexUnavailable, get_gateway

log = logging.getLogger(__name__)

#: Plex mag keine endlos langen URLs – Items werden in Häppchen hinzugefügt.
PLAYLIST_CHUNK_SIZE = 100

_MOVIE_RANK = 0
_EPISODE_RANK = 1


# ---------------------------------------------------------------------------
# Datentransfer
# ---------------------------------------------------------------------------


@dataclass
class PreviewItem:
    """Ein Treffer der Zeitreise – Film oder Episode, UI-fertig aufbereitet."""

    rating_key: str
    blacklist_key: str
    blacklist_type: str  # "movie" | "show"
    media_type: str  # "movie" | "episode"
    title: str
    series_title: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    air_date: Optional[date] = None
    year: Optional[int] = None
    thumb: str = ""
    duration_minutes: Optional[int] = None
    plex_object: Any = field(default=None, repr=False, compare=False)

    @property
    def is_episode(self) -> bool:
        return self.media_type == "episode"

    @property
    def display_title(self) -> str:
        if not self.is_episode:
            return self.title
        code = ""
        if self.season is not None and self.episode is not None:
            code = f"S{self.season:02d}E{self.episode:02d} "
        return f"{self.series_title} – {code}{self.title}".strip()

    @property
    def air_date_display(self) -> str:
        return format_date(self.air_date)

    @property
    def sort_key(self) -> tuple:
        return (
            self.air_date or date.max,
            _EPISODE_RANK if self.is_episode else _MOVIE_RANK,
            (self.series_title or self.title).lower(),
            self.season if self.season is not None else -1,
            self.episode if self.episode is not None else -1,
            self.title.lower(),
        )


@dataclass
class PreviewResult:
    """Ergebnis einer Vorschau-Berechnung."""

    items: list[PreviewItem] = field(default_factory=list)
    total: int = 0
    blacklisted: int = 0
    movies: int = 0
    episodes: int = 0
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class SyncResult:
    """Ergebnis eines Playlist-Syncs."""

    user_id: str
    playlist_name: str = ""
    item_count: int = 0
    trigger: str = "manual"
    changed: bool = True
    message: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# ---------------------------------------------------------------------------
# Plex -> PreviewItem
# ---------------------------------------------------------------------------


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_preview_item(obj: Any) -> Optional[PreviewItem]:
    """Wandelt ein plexapi-Video in ein PreviewItem. None => unbrauchbar."""
    media_type = getattr(obj, "type", "") or ""
    rating_key = getattr(obj, "ratingKey", None)
    if rating_key is None or media_type not in {"movie", "episode"}:
        return None

    air_date = _as_date(getattr(obj, "originallyAvailableAt", None))
    duration = _as_int(getattr(obj, "duration", None))

    if media_type == "episode":
        series_key = getattr(obj, "grandparentRatingKey", None)
        return PreviewItem(
            rating_key=str(rating_key),
            blacklist_key=str(series_key if series_key is not None else rating_key),
            blacklist_type="show",
            media_type="episode",
            title=getattr(obj, "title", "") or "",
            series_title=getattr(obj, "grandparentTitle", "") or "",
            season=_as_int(getattr(obj, "parentIndex", None)),
            episode=_as_int(getattr(obj, "index", None)),
            air_date=air_date,
            year=_as_int(getattr(obj, "year", None)),
            thumb=getattr(obj, "grandparentThumb", None) or getattr(obj, "thumb", "") or "",
            duration_minutes=duration // 60000 if duration else None,
            plex_object=obj,
        )

    return PreviewItem(
        rating_key=str(rating_key),
        blacklist_key=str(rating_key),
        blacklist_type="movie",
        media_type="movie",
        title=getattr(obj, "title", "") or "",
        air_date=air_date,
        year=_as_int(getattr(obj, "year", None)),
        thumb=getattr(obj, "thumb", "") or "",
        duration_minutes=duration // 60000 if duration else None,
        plex_object=obj,
    )


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def _date_filters(start: date, end: date) -> dict[str, str]:
    """Plex kennt bei Datumsfeldern nur ``is before``/``is after`` (exklusiv).

    Deshalb wird der Bereich um je einen Tag geweitet; die exakte Eingrenzung
    passiert anschliessend clientseitig in :func:`_in_range`.
    """
    from datetime import timedelta

    return {
        "originallyAvailableAt>>": (start - timedelta(days=1)).strftime("%Y-%m-%d"),
        "originallyAvailableAt<<": (end + timedelta(days=1)).strftime("%Y-%m-%d"),
    }


def _in_range(item: PreviewItem, start: date, end: date) -> bool:
    return item.air_date is not None and start <= item.air_date <= end


def _search_section(section: Any, start: date, end: date, libtype: str) -> list[Any]:
    """Serverseitige Suche mit clientseitigem Fallback."""
    filters = dict(_date_filters(start, end))
    filters["unwatched"] = True
    try:
        return list(section.search(libtype=libtype, filters=filters))
    except Exception as exc:
        log.warning(
            "Serverseitiger Filter für '%s' fehlgeschlagen (%s) – nutze Fallback",
            section.title,
            exc,
        )
    # Fallback: nur ungesehene Items holen und lokal nach Datum filtern.
    return list(section.search(libtype=libtype, unwatched=True))


def collect_items(
    gateway: PlexGateway, server: Any, start: date, end: date
) -> list[PreviewItem]:
    """Alle ungesehenen Filme + Episoden im Zeitraum, chronologisch sortiert."""
    movies = _search_section(gateway.movie_section(server), start, end, "movie")
    episodes = _search_section(gateway.tv_section(server), start, end, "episode")

    items: list[PreviewItem] = []
    for obj in list(movies) + list(episodes):
        item = to_preview_item(obj)
        if item is not None and _in_range(item, start, end):
            items.append(item)

    items.sort(key=lambda i: i.sort_key)
    return items


def apply_blacklist(
    items: Iterable[PreviewItem], keys: set[str]
) -> tuple[list[PreviewItem], int]:
    """Filtert Filme nach ratingKey und Episoden nach grandparentRatingKey."""
    kept: list[PreviewItem] = []
    dropped = 0
    for item in items:
        if item.blacklist_key in keys or item.rating_key in keys:
            dropped += 1
        else:
            kept.append(item)
    return kept, dropped


# ---------------------------------------------------------------------------
# Vorschau + Sync
# ---------------------------------------------------------------------------


def build_preview(
    session: Session,
    user_id: str,
    start: date,
    end: date,
    gateway: Optional[PlexGateway] = None,
    limit: Optional[int] = None,
) -> PreviewResult:
    """Live-Vorschau – berührt Plex nur lesend."""
    gateway = gateway or get_gateway()
    if end < start:
        start, end = end, start

    try:
        server = gateway.connect_as(user_id)
        raw = collect_items(gateway, server, start, end)
    except PlexUnavailable as exc:
        return PreviewResult(error=str(exc))
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Vorschau fehlgeschlagen")
        return PreviewResult(error=f"Unerwarteter Fehler: {exc}")

    keys = db.blacklist_keys(session, user_id)
    items, dropped = apply_blacklist(raw, keys)

    if limit is None:
        limit = get_settings().preview_limit
    truncated = bool(limit) and len(items) > limit
    shown = items[:limit] if truncated else items

    return PreviewResult(
        items=shown,
        total=len(items),
        blacklisted=dropped,
        movies=sum(1 for i in items if not i.is_episode),
        episodes=sum(1 for i in items if i.is_episode),
        truncated=truncated,
    )


def _find_playlist(server: Any, name: str) -> Optional[Any]:
    for playlist in server.playlists():
        if playlist.title == name:
            return playlist
    return None


def _chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def apply_playlist(server: Any, name: str, objects: Sequence[Any]) -> bool:
    """Die feste Playlist leeren und neu befüllen.

    Rückgabe: True, wenn die Playlist danach existiert.  Eine Playlist ohne
    Items kann Plex nicht halten – in dem Fall wird sie gelöscht.
    """
    playlist = _find_playlist(server, name)

    if not objects:
        if playlist is not None:
            playlist.delete()
        return False

    if playlist is not None:
        current = list(playlist.items())
        if current:
            playlist.removeItems(current)
            # Plex entfernt leere Playlists teilweise selbst -> neu suchen.
            playlist = _find_playlist(server, name)

    head, *tail = list(_chunked(list(objects), PLAYLIST_CHUNK_SIZE))
    if playlist is None:
        playlist = server.createPlaylist(name, items=list(head))
    else:
        playlist.addItems(list(head))
    for chunk in tail:
        playlist.addItems(list(chunk))
    return True


def sync_user(
    session: Session,
    user_id: str,
    trigger: str = "manual",
    gateway: Optional[PlexGateway] = None,
) -> SyncResult:
    """Kompletter Sync für einen Home-User – die eigentliche Zeitreise."""
    gateway = gateway or get_gateway()
    state = db.get_or_create_user_state(session, user_id)
    playlist_name = state.target_playlist_name or get_settings().playlist_name_for(user_id)

    if not state.has_period:
        return SyncResult(
            user_id=user_id,
            playlist_name=playlist_name,
            trigger=trigger,
            changed=False,
            error="Kein Zeitraum gewählt – bitte zuerst ein Ziel-Datum setzen.",
        )

    start, end = state.current_date_start, state.current_date_end

    try:
        server = gateway.connect_as(user_id)
        raw = collect_items(gateway, server, start, end)
        items, dropped = apply_blacklist(raw, db.blacklist_keys(session, user_id))
        exists = apply_playlist(server, playlist_name, [i.plex_object for i in items])
    except PlexUnavailable as exc:
        return SyncResult(
            user_id=user_id, playlist_name=playlist_name, trigger=trigger, error=str(exc)
        )
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Sync für %s fehlgeschlagen", user_id)
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

    note = f"{dropped} durch Blacklist ausgeschlossen" if dropped else ""
    db.log_journey(session, user_id, start, end, len(items), trigger=trigger, note=note)

    if exists:
        message = f"{len(items)} Titel in »{playlist_name}« gespeichert."
    else:
        message = (
            f"Keine passenden Titel – »{playlist_name}« wurde geleert bzw. entfernt."
        )

    return SyncResult(
        user_id=user_id,
        playlist_name=playlist_name,
        item_count=len(items),
        trigger=trigger,
        message=message,
    )


def sync_all_users(
    session: Session, trigger: str = "poll", gateway: Optional[PlexGateway] = None
) -> list[SyncResult]:
    """Alle Nutzer mit gesetztem Zeitraum nachziehen (Scheduler/Webhook)."""
    results: list[SyncResult] = []
    for state in db.list_user_states(session):
        if not state.has_period:
            continue
        results.append(
            sync_user(session, state.plex_user_id, trigger=trigger, gateway=gateway)
        )
    return results
