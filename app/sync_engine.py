"""Sync-Engine: Suche, Blacklist-Filter, Merge/Sort und Playlist-Pflege."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from sqlmodel import Session

from app import covers, db
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
    parent_thumb: str = ""   # bei Episoden das Staffelposter
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
            parent_thumb=getattr(obj, "parentThumb", None) or "",
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

    # isoformat() statt strftime(): strftime lässt bei Jahren unter 1000 die
    # führenden Nullen weg ("200-02-06"), was Plex als Filterwert ablehnt – und
    # die Suche in einen Vollscan der Bibliothek zwingt.
    return {
        "originallyAvailableAt>>": (start - timedelta(days=1)).isoformat(),
        "originallyAvailableAt<<": (end + timedelta(days=1)).isoformat(),
    }


def _in_range(item: PreviewItem, start: date, end: date) -> bool:
    return item.air_date is not None and start <= item.air_date <= end


def is_unwatched(obj: Any) -> bool:
    """Ungesehen heißt: Plex hat für dieses Item keinen Abspielvorgang gezählt.

    Der serverseitige ``unwatched``-Filter wird zwar mitgeschickt, aber nicht
    blind geglaubt: greift er nicht (ältere Plex-Fassungen, Fallback-Suche),
    blieben sonst gesehene Titel für immer in der Playlist stehen.
    """
    return not getattr(obj, "viewCount", 0)


def _search_section(section: Any, start: date, end: date, libtype: str) -> list[Any]:
    """Suche mit drei Stufen, damit sie auf jeder Plex-Fassung etwas liefert.

    1. Datum und ``unwatched`` serverseitig – schnell und schonend.
    2. Nur ``unwatched`` – falls Plex die Datumsfilter nicht mag.
    3. Alles – falls Plex auch damit nichts anfangen kann.

    Eingegrenzt wird anschließend ohnehin clientseitig (Datum *und*
    Watch-Status), die Stufen sind reine Mengenbegrenzung.
    """
    filters = dict(_date_filters(start, end))
    filters["unwatched"] = True
    versuche = (
        ("Datum + ungesehen", lambda: section.search(libtype=libtype, filters=filters)),
        ("nur ungesehen", lambda: section.search(libtype=libtype, unwatched=True)),
        ("ohne Filter", lambda: section.search(libtype=libtype)),
    )

    letzter_fehler: Optional[Exception] = None
    for beschreibung, versuch in versuche:
        try:
            return list(versuch())
        except Exception as exc:
            letzter_fehler = exc
            log.warning(
                "Suche in '%s' (%s) fehlgeschlagen: %s – nächste Stufe",
                section.title,
                beschreibung,
                exc,
            )
    raise RuntimeError(
        f"Bibliothek '{section.title}' ließ sich nicht durchsuchen: {letzter_fehler}"
    )


def collect_items(
    gateway: PlexGateway, server: Any, start: date, end: date
) -> list[PreviewItem]:
    """Alle ungesehenen Filme + Episoden im Zeitraum, chronologisch sortiert."""
    movies = _search_section(gateway.movie_section(server), start, end, "movie")
    episodes = _search_section(gateway.tv_section(server), start, end, "episode")

    items: list[PreviewItem] = []
    for obj in list(movies) + list(episodes):
        if not is_unwatched(obj):
            continue
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


def rename_playlist_on(server: Any, old_name: str, new_name: str) -> bool:
    """Eine vorhandene Plex-Playlist umbenennen (falls es sie unter dem alten
    Namen noch gibt)."""
    if not old_name or old_name == new_name:
        return False
    for playlist in server.playlists():
        if playlist.title == old_name:
            playlist.editTitle(new_name)
            return True
    return False


def _chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class PlaylistOutcome:
    """Was beim Schreiben der Playlist herauskam."""

    playlist: Any = field(default=None, repr=False)
    created: bool = False
    unchanged: bool = False

    @property
    def exists(self) -> bool:
        return self.playlist is not None


def _rating_keys(objects: Iterable[Any]) -> list[str]:
    return [str(getattr(obj, "ratingKey", "")) for obj in objects]


def apply_playlist(server: Any, name: str, objects: Sequence[Any]) -> PlaylistOutcome:
    """Die feste Playlist leeren und neu befüllen.

    Eine Playlist ohne Items kann Plex nicht halten – in dem Fall wird sie
    gelöscht. ``created`` sagt, ob die Playlist neu angelegt wurde; nur dann
    muss ein Cover erneut übertragen werden.
    """
    playlist = _find_playlist(server, name)

    if not objects:
        if playlist is not None:
            playlist.delete()
        return PlaylistOutcome()

    if playlist is not None:
        current = list(playlist.items())
        if _rating_keys(current) == _rating_keys(objects):
            # Nichts zu tun. Das ist der Normalfall beim Polling – und spart
            # richtig etwas: plexapi löscht Playlist-Einträge einzeln, eine
            # 800er-Playlist würde sonst bei jedem Lauf 800 Anfragen kosten.
            log.debug("Playlist '%s' ist unverändert – kein Neuschreiben", name)
            return PlaylistOutcome(playlist=playlist, unchanged=True)
        if current:
            playlist.removeItems(current)
            # Plex entfernt leere Playlists teilweise selbst -> neu suchen.
            playlist = _find_playlist(server, name)

    created = playlist is None
    head, *tail = list(_chunked(list(objects), PLAYLIST_CHUNK_SIZE))
    if playlist is None:
        playlist = server.createPlaylist(name, items=list(head))
    else:
        playlist.addItems(list(head))
    for chunk in tail:
        playlist.addItems(list(chunk))
    return PlaylistOutcome(playlist=playlist, created=created)


# ---------------------------------------------------------------------------
# Cover
# ---------------------------------------------------------------------------


def upload_cover(playlist: Any, cover_file: Any) -> None:
    """Cover als Poster an Plex schicken (die Bytes gehen mit, kein Pfad)."""
    playlist.uploadPoster(filepath=str(cover_file))


def apply_cover_after_sync(outcome: PlaylistOutcome, cover_path: Optional[str],
                           already_applied: bool) -> bool:
    """Cover übertragen, wenn die Playlist neu ist oder es noch nie dran war.

    Fehler bleiben folgenlos: die Playlist steht, das Poster kommt beim
    nächsten Lauf erneut dran.
    """
    cover_file = covers.path_for(cover_path)
    if not outcome.exists or cover_file is None:
        return False
    if already_applied and not outcome.created:
        return False
    try:
        upload_cover(outcome.playlist, cover_file)
    except Exception as exc:  # pragma: no cover - Plex kann ablehnen
        log.warning("Cover nicht übertragen: %s", exc)
        return False
    return True


def push_cover(
    gateway: PlexGateway, user_id: str, playlist_name: str, cover_path: Optional[str]
) -> bool:
    """Cover sofort übertragen – für den Moment, in dem es hochgeladen wird."""
    cover_file = covers.path_for(cover_path)
    if cover_file is None:
        return False
    server = gateway.connect_as(user_id)
    playlist = _find_playlist(server, playlist_name)
    if playlist is None:
        return False  # Playlist gibt es noch nicht; der nächste Sync erledigt es
    upload_cover(playlist, cover_file)
    return True


def clear_cover(gateway: PlexGateway, user_id: str, playlist_name: str) -> bool:
    """Poster in Plex wieder entfernen."""
    server = gateway.connect_as(user_id)
    playlist = _find_playlist(server, playlist_name)
    if playlist is None:
        return False
    playlist.deletePoster()
    return True


def _with_transitions(session, user_id: str, items, server) -> list:
    """Übergangsclips vor die jeweiligen Tage setzen, sofern vorhanden.

    Import bewusst hier drin: transition_build baut auf diesem Modul auf, ein
    Import am Kopf wäre ein Ring.
    """
    if not get_settings().transitions_for(user_id) or not items:
        return [i.plex_object for i in items]

    from app import transition_build

    try:
        clips = transition_build.clips_for_playlist(session, user_id, server)
    except Exception as exc:  # pragma: no cover - Plex kann zicken
        log.warning("Übergänge konnten nicht eingefügt werden: %s", exc)
        return [i.plex_object for i in items]

    if not clips:
        return [i.plex_object for i in items]
    return transition_build.interleave(items, clips)


def _request_transition_build(session, user_id: str, items, period) -> None:
    """Falls für diesen Zeitraum noch keine Clips existieren: im Hintergrund
    erzeugen lassen. Das Rendern dauert Minuten und darf den Lauf nicht
    aufhalten."""
    if not get_settings().transitions_for(user_id) or not items:
        return

    from app import transition_build
    from app.scheduler import get_scheduler

    if not transition_build.needs_rebuild(session, user_id, period):
        return
    scheduler = get_scheduler()
    if scheduler is None:
        log.warning("Kein Scheduler aktiv – Übergänge für %s bleiben aus", user_id)
        return
    scheduler.request_transition_build(user_id)


def sync_user(
    session: Session,
    user_id: str,
    trigger: str = "manual",
    gateway: Optional[PlexGateway] = None,
) -> SyncResult:
    """Kompletter Sync für einen Home-User – die eigentliche Zeitreise."""
    gateway = gateway or get_gateway()
    state = db.get_or_create_user_state(session, user_id)
    bisheriger_name = state.target_playlist_name

    if not state.has_period:
        return SyncResult(
            user_id=user_id,
            playlist_name=bisheriger_name,
            trigger=trigger,
            changed=False,
            error="Kein Zeitraum gewählt – bitte zuerst ein Ziel-Datum setzen.",
        )

    start, end = state.current_date_start, state.current_date_end
    blacklist = db.blacklist_keys(session, user_id)
    # Transaktion schließen, bevor es zu Plex geht: sonst liegt die Datenbank
    # für die Dauer der Abfragen fest und die Oberfläche läuft in Sperren.
    session.commit()

    try:
        server = gateway.connect_as(user_id)
        raw = collect_items(gateway, server, start, end)
        items, dropped = apply_blacklist(raw, blacklist)

        # Der Name richtet sich nach dem ältesten Titel, der noch drin ist.
        # Ist der erste Tag weggesehen, rückt das Datum von selbst weiter –
        # deshalb steht die Umbenennung hier, im selben Lauf wie das Aufräumen.
        first_date = items[0].air_date if items else start
        playlist_name = get_settings().playlist_name_for(user_id, first_date)
        if bisheriger_name and bisheriger_name != playlist_name:
            if rename_playlist_on(server, bisheriger_name, playlist_name):
                log.info(
                    "Playlist umbenannt: »%s« -> »%s«", bisheriger_name, playlist_name
                )

        eintraege = _with_transitions(session, user_id, items, server)
        outcome = apply_playlist(server, playlist_name, eintraege)
        cover_done = apply_cover_after_sync(
            outcome, state.cover_path, state.cover_applied_at is not None
        )
    except PlexUnavailable as exc:
        return SyncResult(
            user_id=user_id,
            playlist_name=bisheriger_name,
            trigger=trigger,
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover - unerwartete plexapi-Fehler
        log.exception("Sync für %s fehlgeschlagen", user_id)
        return SyncResult(
            user_id=user_id,
            playlist_name=bisheriger_name,
            trigger=trigger,
            error=f"Unerwarteter Fehler: {exc}",
        )

    state.last_synced_at = db.utcnow()
    state.last_item_count = len(items)
    state.target_playlist_name = playlist_name
    if cover_done:
        state.cover_applied_at = db.utcnow()
    session.add(state)
    session.commit()

    note = f"{dropped} durch Blacklist ausgeschlossen" if dropped else ""
    db.log_journey(session, user_id, start, end, len(items), trigger=trigger, note=note)
    _request_transition_build(session, user_id, items, (start, end))

    if outcome.exists:
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
