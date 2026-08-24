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

from app import covers, db
from app.config import get_settings
from app.plex_client import PlexGateway, PlexUnavailable, get_gateway
from app.sync_engine import (
    PreviewItem,
    PreviewResult,
    SyncResult,
    apply_cover_after_sync,
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
    almanach: db.Almanach,
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
        server = gateway.connect_as(almanach.plex_user_id)
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

    chosen = db.almanach_keys(session, almanach.id)
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
    almanach: db.Almanach,
    gateway: Optional[PlexGateway] = None,
    limit: Optional[int] = None,
) -> PreviewResult:
    """Vorschau der Almanach-Playlist – ohne etwas in Plex zu verändern."""
    entries = db.list_almanach_entries(session, almanach.id)
    if not entries:
        return PreviewResult()

    gateway = gateway or get_gateway()
    try:
        server = gateway.connect_as(almanach.plex_user_id)
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
    almanach: db.Almanach,
    trigger: str = "manual",
    gateway: Optional[PlexGateway] = None,
) -> SyncResult:
    """Playlist eines benannten Almanachs neu aufbauen."""
    gateway = gateway or get_gateway()
    user_id = almanach.plex_user_id
    playlist_name = almanach.target_playlist_name or get_settings().almanach_playlist_name_for(
        user_id, almanach.name
    )
    entries = db.list_almanach_entries(session, almanach.id)

    if not entries:
        return SyncResult(
            user_id=user_id,
            playlist_name=playlist_name,
            trigger=trigger,
            changed=False,
            error=f"»{almanach.name}« ist leer – bitte zuerst Serien oder Filme aufnehmen.",
        )

    try:
        server = gateway.connect_as(user_id)
        items, missing = collect_almanach_items(server, entries)
        outcome = apply_playlist(server, playlist_name, [i.plex_object for i in items])
        cover_done = apply_cover_after_sync(
            outcome, almanach.cover_path, almanach.cover_applied_at is not None
        )
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

    almanach.last_synced_at = db.utcnow()
    almanach.last_item_count = len(items)
    almanach.target_playlist_name = playlist_name
    if cover_done:
        almanach.cover_applied_at = db.utcnow()
    session.add(almanach)
    session.commit()

    note = almanach.name
    if missing:
        note += f" · {len(missing)} Einträge nicht mehr in der Bibliothek"
    db.log_journey(
        session, user_id, None, None, len(items), trigger=trigger, note=note, kind="almanach"
    )

    if outcome.exists:
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
    """Jeden gefüllten Almanach nachziehen (Scheduler/Webhook)."""
    return [
        sync_almanach(session, almanach, trigger=trigger, gateway=gateway)
        for almanach in db.almanachs_with_entries(session)
    ]


def rename_playlist(
    almanach: db.Almanach, old_name: str, gateway: Optional[PlexGateway] = None
) -> bool:
    """Die vorhandene Plex-Playlist mit umbenennen.

    Sonst bliebe die alte Playlist unter dem alten Namen liegen und der nächste
    Sync legte eine zweite an.
    """
    new_name = almanach.target_playlist_name
    if not old_name or old_name == new_name:
        return False
    gateway = gateway or get_gateway()
    server = gateway.connect_as(almanach.plex_user_id)
    for playlist in server.playlists():
        if playlist.title == old_name:
            playlist.editTitle(new_name)
            return True
    return False


def delete_playlist(almanach: db.Almanach, gateway: Optional[PlexGateway] = None) -> bool:
    """Die Plex-Playlist eines Almanachs entfernen (beim Löschen der Sammlung)."""
    if not almanach.target_playlist_name:
        return False
    gateway = gateway or get_gateway()
    server = gateway.connect_as(almanach.plex_user_id)
    for playlist in server.playlists():
        if playlist.title == almanach.target_playlist_name:
            playlist.delete()
            return True
    return False


# ---------------------------------------------------------------------------
# In andere Profile übernehmen
# ---------------------------------------------------------------------------


@dataclass
class CopyResult:
    """Was beim Übernehmen in ein Profil herauskam."""

    user_id: str
    created: bool = False
    added: int = 0
    total: int = 0
    sync: Optional[SyncResult] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def unchanged(self) -> bool:
        return self.ok and not self.created and self.added == 0


def copy_almanach(
    session: Session,
    source: db.Almanach,
    target_user_id: str,
    gateway: Optional[PlexGateway] = None,
    build: bool = True,
) -> CopyResult:
    """Sammlung in das Profil eines anderen Home-Users übernehmen.

    Die Kopie gehört dem Zielprofil und wird deshalb gegen dessen eigenen
    Watch-Status gebaut – jeder sieht in seiner Playlist nur, was er selbst
    noch nicht gesehen hat.

    Existiert dort bereits eine Sammlung gleichen Namens, wird sie ergänzt
    statt eine zweite anzulegen. Ein erneutes Übernehmen gleicht also nur die
    fehlenden Titel nach.
    """
    if target_user_id == source.plex_user_id:
        return CopyResult(
            user_id=target_user_id, error="Die Sammlung gehört diesem Profil bereits."
        )

    target = db.find_almanach_by_name(session, target_user_id, source.name)
    created = target is None
    if target is None:
        target = db.create_almanach(session, target_user_id, source.name)
        if source.cover_path:
            target.cover_path = covers.copy(
                source.cover_path, covers.almanach_stem(target.id)
            )
            target.cover_applied_at = None
            session.add(target)
            session.commit()

    present = db.almanach_keys(session, target.id)
    added = 0
    for entry in db.list_almanach_entries(session, source.id):
        if entry.plex_rating_key in present:
            continue
        db.add_to_almanach(
            session,
            target,
            entry.plex_rating_key,
            entry.media_type,
            entry.title,
            entry.year,
        )
        added += 1

    result = CopyResult(
        user_id=target_user_id,
        created=created,
        added=added,
        total=len(db.list_almanach_entries(session, target.id)),
    )
    if build:
        result.sync = sync_almanach(session, target, trigger="kopie", gateway=gateway)
    return result


def copy_almanach_to_users(
    session: Session,
    source: db.Almanach,
    target_user_ids: Iterable[str],
    gateway: Optional[PlexGateway] = None,
    build: bool = True,
) -> list[CopyResult]:
    return [
        copy_almanach(session, source, user_id, gateway=gateway, build=build)
        for user_id in target_user_ids
    ]


# ---------------------------------------------------------------------------
# Watch-Status zurücksetzen
# ---------------------------------------------------------------------------


@dataclass
class ResetPlan:
    """Was ein Reset anfassen würde – Grundlage für die Rückfrage."""

    watched_movies: int = 0
    watched_episodes: int = 0
    total_movies: int = 0
    total_episodes: int = 0
    missing: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def watched_total(self) -> int:
        return self.watched_movies + self.watched_episodes

    @property
    def nothing_to_do(self) -> bool:
        return self.ok and self.watched_total == 0


@dataclass
class ResetResult:
    """Ergebnis eines ausgeführten Resets."""

    movies: int = 0
    episodes: int = 0
    missing: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def total(self) -> int:
        return self.movies + self.episodes


def _plan_for_entry(obj: Any) -> tuple[int, int, int, int]:
    """(gesehene Filme, gesehene Episoden, Filme gesamt, Episoden gesamt)."""
    if getattr(obj, "type", "") == "show":
        total = _as_int(getattr(obj, "leafCount", None)) or 0
        watched = _as_int(getattr(obj, "viewedLeafCount", None)) or 0
        return 0, watched, 0, total
    return (1 if getattr(obj, "viewCount", 0) else 0), 0, 1, 0


def plan_reset(
    session: Session, almanach: db.Almanach, gateway: Optional[PlexGateway] = None
) -> ResetPlan:
    """Zählen, was ein Reset zurücksetzen würde – verändert nichts."""
    entries = db.list_almanach_entries(session, almanach.id)
    if not entries:
        return ResetPlan(error=f"»{almanach.name}« ist leer – nichts zurückzusetzen.")

    gateway = gateway or get_gateway()
    plan = ResetPlan()
    try:
        server = gateway.connect_as(almanach.plex_user_id)
        for entry in entries:
            try:
                obj = server.fetchItem(int(entry.plex_rating_key))
            except (NotFound, ValueError):
                plan.missing.append(entry.title or entry.plex_rating_key)
                continue
            movies, episodes, total_movies, total_episodes = _plan_for_entry(obj)
            plan.watched_movies += movies
            plan.watched_episodes += episodes
            plan.total_movies += total_movies
            plan.total_episodes += total_episodes
    except PlexUnavailable as exc:
        return ResetPlan(error=str(exc))
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Reset-Vorschau fehlgeschlagen")
        return ResetPlan(error=f"Unerwarteter Fehler: {exc}")

    return plan


def reset_watch_state(
    session: Session, almanach: db.Almanach, gateway: Optional[PlexGateway] = None
) -> ResetResult:
    """Alle Filme und Episoden des Almanachs auf »ungesehen« setzen.

    Serien werden in einem Rutsch zurückgesetzt (``markUnplayed`` auf der Serie
    wirkt auf alle Episoden); gezählt wird, was vorher als gesehen galt.
    """
    entries = db.list_almanach_entries(session, almanach.id)
    if not entries:
        return ResetResult(error=f"»{almanach.name}« ist leer – nichts zurückzusetzen.")

    gateway = gateway or get_gateway()
    result = ResetResult()
    try:
        server = gateway.connect_as(almanach.plex_user_id)
        for entry in entries:
            try:
                obj = server.fetchItem(int(entry.plex_rating_key))
            except (NotFound, ValueError):
                result.missing.append(entry.title or entry.plex_rating_key)
                continue

            movies, episodes, _, _ = _plan_for_entry(obj)
            obj.markUnplayed()
            result.movies += movies
            result.episodes += episodes
    except PlexUnavailable as exc:
        return ResetResult(error=str(exc))
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Reset fehlgeschlagen")
        return ResetResult(error=f"Unerwarteter Fehler: {exc}")

    log.info(
        "Watch-Status zurückgesetzt: %s Filme, %s Episoden (Almanach »%s«, Nutzer %s)",
        result.movies,
        result.episodes,
        almanach.name,
        almanach.plex_user_id,
    )
    return result
