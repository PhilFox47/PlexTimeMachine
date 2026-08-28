"""SQLite-Persistenz: UserState, BlacklistEntry, JourneyLog."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from enum import Enum
from typing import Iterator, Optional, Sequence

from sqlalchemy import event, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.config import get_settings

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Zeitzonenloser UTC-Zeitstempel (SQLite speichert ohnehin naiv)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MediaType(str, Enum):
    movie = "movie"
    show = "show"


class UserState(SQLModel, table=True):
    """Der aktuell gewählte Zeitraum eines Home-Users – genau ein Eintrag pro Nutzer."""

    __tablename__ = "user_state"

    plex_user_id: str = Field(primary_key=True, description="ID/Titel des Home-Users")
    current_date_start: Optional[date] = None
    current_date_end: Optional[date] = None
    target_playlist_name: str = ""
    last_synced_at: Optional[datetime] = None
    last_item_count: int = 0
    cover_path: Optional[str] = None
    cover_applied_at: Optional[datetime] = None

    @property
    def has_period(self) -> bool:
        return self.current_date_start is not None and self.current_date_end is not None


class BlacklistEntry(SQLModel, table=True):
    """Dauerhaft ausgeschlossene Serie/Film – gilt pro Nutzer."""

    __tablename__ = "blacklist_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    plex_user_id: str = Field(index=True)
    plex_rating_key: str = Field(index=True)
    media_type: MediaType = MediaType.movie
    title: str = ""
    added_at: datetime = Field(default_factory=utcnow)

    @property
    def is_show(self) -> bool:
        return MediaType(self.media_type) is MediaType.show


class Almanach(SQLModel, table=True):
    """Eine benannte Sammlung: der Inhalt, den sich mehrere Profile teilen.

    Die Einträge (und das Cover) hängen an dieser Zeile und gelten für alle
    freigegebenen Profile. Gepflegt werden sie vom Eigentümer.
    """

    __tablename__ = "almanach"

    id: Optional[int] = Field(default=None, primary_key=True)
    plex_user_id: str = Field(index=True)  # Eigentümer: pflegt den Inhalt
    name: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    cover_path: Optional[str] = None


class AlmanachShare(SQLModel, table=True):
    """Die Playlist eines Profils zu einer Sammlung – der eigene Fortschritt.

    Für jedes freigegebene Profil (den Eigentümer eingeschlossen) gibt es genau
    eine Zeile. Der Inhalt kommt aus dem Almanach, gebaut wird er aber im
    Kontext dieses Profils – der Watch-Status bleibt damit persönlich.
    """

    __tablename__ = "almanach_share"

    id: Optional[int] = Field(default=None, primary_key=True)
    almanach_id: int = Field(index=True)
    plex_user_id: str = Field(index=True)
    target_playlist_name: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    last_synced_at: Optional[datetime] = None
    last_item_count: int = 0
    cover_applied_at: Optional[datetime] = None  # je Playlist, nicht je Sammlung


class AlmanachEntry(SQLModel, table=True):
    """Eine bewusst ausgewählte Serie/ein Film innerhalb eines Almanachs."""

    __tablename__ = "almanach_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    almanach_id: Optional[int] = Field(default=None, index=True)
    plex_user_id: str = Field(index=True)
    plex_rating_key: str = Field(index=True)
    media_type: MediaType = MediaType.movie
    title: str = ""
    year: Optional[int] = None
    added_at: datetime = Field(default_factory=utcnow)

    @property
    def is_show(self) -> bool:
        return MediaType(self.media_type) is MediaType.show


class TransitionClip(SQLModel, table=True):
    """Ein erzeugter Übergangsclip – gehört zu einem Nutzer und einem Zeitraum.

    Die Clips werden pro Zeitraum erzeugt: wechselt der Nutzer die Woche,
    fliegen die alten heraus und es entstehen neue.
    """

    __tablename__ = "transition_clip"

    id: Optional[int] = Field(default=None, primary_key=True)
    plex_user_id: str = Field(index=True)
    day: date
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    file_name: str = ""
    title: str = ""          # so heißt der Clip später in Plex
    item_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class JourneyLog(SQLModel, table=True):
    """Logbuch: eine Zeile pro ausgeführter Zeitreise."""

    __tablename__ = "journey_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    plex_user_id: str = Field(index=True)
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    executed_at: datetime = Field(default_factory=utcnow)
    item_count: int = 0
    trigger: str = "manual"
    note: str = ""
    kind: str = "timemachine"  # "timemachine" | "almanach"


_engine = None


def get_engine():
    """Lazy erzeugte Engine – erlaubt Tests, die DATABASE_URL vorher zu setzen."""
    global _engine
    if _engine is None:
        url = get_settings().database_url
        # timeout: wie lange auf eine belegte Datenbank gewartet wird, statt
        # sofort mit "database is locked" abzubrechen (Standard sind 5 s).
        connect_args = {"check_same_thread": False, "timeout": LOCK_TIMEOUT_SECONDS}
        kwargs = {}
        if url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, echo=False, connect_args=connect_args, **kwargs)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _apply_sqlite_pragmas)
    return _engine


#: Solange wartet ein Zugriff, wenn gerade geschrieben wird. Ein langer Sync
#: (große Playlist) darf die Oberfläche nicht mit einem Fehler abwürgen.
LOCK_TIMEOUT_SECONDS = 30


def _apply_sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL und Wartezeit setzen, damit Lesen und Schreiben sich nicht behindern.

    Ohne WAL sperrt ein Schreibvorgang die gesamte Datei: der Hintergrund-Sync
    ließ damit jeden gleichzeitigen Klick in der Oberfläche mit einem
    "database is locked" auflaufen. WAL ist nicht auf jedem Dateisystem
    verfügbar (etwa auf manchen Netzlaufwerken), deshalb nur ein Versuch – die
    Wartezeit allein hilft schon.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {LOCK_TIMEOUT_SECONDS * 1000}")
        cursor.execute("PRAGMA journal_mode = WAL")
        modus = cursor.fetchone()
        if modus and str(modus[0]).lower() != "wal":
            log.warning(
                "SQLite bleibt im Modus '%s' – WAL ist hier nicht verfügbar.", modus[0]
            )
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def reset_engine() -> None:
    """Nur für Tests: Engine verwerfen, damit eine neue URL greift."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def _add_missing_columns(engine) -> list[str]:
    """Fehlende Spalten in bereits bestehenden Tabellen ergänzen.

    SQLite kann Spalten nachträglich anhängen; ``create_all`` tut das nicht.
    Damit überlebt eine bestehende Datenbank ein Update, ohne dass Blacklist
    und Logbuch verloren gehen.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    added: list[str] = []

    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing:
                continue  # legt create_all gleich vollständig an
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} "
                    f"ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
                )
                default = getattr(column.default, "arg", None)
                if isinstance(default, (str, int, float)) and not callable(default):
                    literal = f"'{default}'" if isinstance(default, str) else default
                    ddl += f" DEFAULT {literal}"
                connection.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


def _migrate_legacy_almanach(engine) -> int:
    """Einträge aus der Zeit vor benannten Almanachs einsortieren.

    Bis dahin hatte jeder Nutzer genau einen Almanach ohne Namen. Dessen
    Einträge bekommen jetzt einen echten Almanach – inklusive des bisherigen
    Playlist-Namens, damit die vorhandene Plex-Playlist weiterverwendet wird.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "almanach_entry" not in tables:
        return 0

    legacy_state: dict[str, tuple] = {}
    if "almanach_state" in tables:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT plex_user_id, target_playlist_name, last_synced_at, "
                    "last_item_count FROM almanach_state"
                )
            ).all()
        legacy_state = {row[0]: row for row in rows}

    migrated = 0
    with Session(engine) as session:
        orphans = session.exec(
            select(AlmanachEntry).where(AlmanachEntry.almanach_id.is_(None))
        ).all()
        by_user: dict[str, list[AlmanachEntry]] = {}
        for entry in orphans:
            by_user.setdefault(entry.plex_user_id, []).append(entry)

        for user_id, entries in by_user.items():
            almanach = Almanach(plex_user_id=user_id, name="Mein Almanach")
            session.add(almanach)
            session.commit()
            session.refresh(almanach)

            state = legacy_state.get(user_id)
            session.add(
                AlmanachShare(
                    almanach_id=almanach.id,
                    plex_user_id=user_id,
                    target_playlist_name=(state[1] if state and state[1] else "")
                    or get_settings().almanach_playlist_name_for(user_id, almanach.name),
                    last_item_count=(state[3] if state else 0) or 0,
                )
            )
            session.commit()

            for entry in entries:
                entry.almanach_id = almanach.id
                session.add(entry)
            session.commit()
            migrated += len(entries)

    return migrated


def _migrate_almanach_shares(engine) -> int:
    """Jede Sammlung ohne Freigabe-Zeile bekommt eine für ihren Eigentümer.

    Vor dem Umbau steckte der Playlist-Zustand direkt in der Sammlung. Diese
    Spalten gibt es in alten Datenbanken noch – ihre Werte wandern in die
    Freigabe, damit die bestehende Plex-Playlist weiterverwendet wird.
    """
    inspector = inspect(engine)
    if "almanach" not in set(inspector.get_table_names()):
        return 0

    columns = {column["name"] for column in inspector.get_columns("almanach")}
    legacy = {"target_playlist_name", "last_synced_at", "last_item_count"} <= columns

    old_state: dict[int, tuple] = {}
    if legacy:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, target_playlist_name, last_synced_at, last_item_count "
                    "FROM almanach"
                )
            ).all()
        old_state = {row[0]: row for row in rows}

    migrated = 0
    with Session(engine) as session:
        for almanach in session.exec(select(Almanach)).all():
            if get_share(session, almanach.id, almanach.plex_user_id) is not None:
                continue
            state = old_state.get(almanach.id)
            share = AlmanachShare(
                almanach_id=almanach.id,
                plex_user_id=almanach.plex_user_id,
                target_playlist_name=(state[1] if state and state[1] else "")
                or get_settings().almanach_playlist_name_for(
                    almanach.plex_user_id, almanach.name
                ),
                last_item_count=(state[3] if state else 0) or 0,
            )
            session.add(share)
            session.commit()
            migrated += 1
    return migrated


def init_db() -> None:
    engine = get_engine()
    _add_missing_columns(engine)
    SQLModel.metadata.create_all(engine)
    _migrate_legacy_almanach(engine)
    _migrate_almanach_shares(engine)


def get_session() -> Iterator[Session]:
    # expire_on_commit=False: nach einem commit bleiben geladene Objekte
    # benutzbar. Erst dadurch kann die Sync-Logik ihre Transaktion beenden,
    # bevor sie minutenlang mit Plex spricht.
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


# ---------------------------------------------------------------------------
# Repository-Funktionen
# ---------------------------------------------------------------------------


def get_or_create_user_state(session: Session, user_id: str) -> UserState:
    state = session.get(UserState, user_id)
    if state is None:
        # Der Playlist-Name richtet sich nach dem ältesten Titel der Playlist
        # und steht deshalb erst nach dem ersten Lauf fest.
        state = UserState(plex_user_id=user_id)
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


def set_period(session: Session, user_id: str, start: date, end: date) -> UserState:
    if end < start:
        start, end = end, start
    state = get_or_create_user_state(session, user_id)
    state.current_date_start = start
    state.current_date_end = end
    session.add(state)
    session.commit()
    session.refresh(state)
    return state


def list_user_states(session: Session) -> Sequence[UserState]:
    return session.exec(select(UserState).order_by(UserState.plex_user_id)).all()


def list_blacklist(session: Session, user_id: str) -> Sequence[BlacklistEntry]:
    stmt = (
        select(BlacklistEntry)
        .where(BlacklistEntry.plex_user_id == user_id)
        .order_by(BlacklistEntry.added_at.desc())
    )
    return session.exec(stmt).all()


def blacklist_keys(session: Session, user_id: str) -> set[str]:
    stmt = select(BlacklistEntry.plex_rating_key).where(
        BlacklistEntry.plex_user_id == user_id
    )
    return {str(key) for key in session.exec(stmt).all()}


def add_to_blacklist(
    session: Session,
    user_id: str,
    rating_key: str,
    media_type: MediaType | str,
    title: str = "",
) -> BlacklistEntry:
    rating_key = str(rating_key)
    existing = session.exec(
        select(BlacklistEntry).where(
            BlacklistEntry.plex_user_id == user_id,
            BlacklistEntry.plex_rating_key == rating_key,
        )
    ).first()
    if existing:
        if title and existing.title != title:
            existing.title = title
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    entry = BlacklistEntry(
        plex_user_id=user_id,
        plex_rating_key=rating_key,
        media_type=MediaType(media_type),
        title=title,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def remove_from_blacklist(session: Session, user_id: str, rating_key: str) -> bool:
    entry = session.exec(
        select(BlacklistEntry).where(
            BlacklistEntry.plex_user_id == user_id,
            BlacklistEntry.plex_rating_key == str(rating_key),
        )
    ).first()
    if entry is None:
        return False
    session.delete(entry)
    session.commit()
    return True


def create_almanach(session: Session, user_id: str, name: str) -> Almanach:
    """Sammlung anlegen – der Eigentümer bekommt gleich seine eigene Playlist."""
    almanach = Almanach(plex_user_id=user_id, name=name.strip() or "Ohne Namen")
    session.add(almanach)
    session.commit()
    session.refresh(almanach)
    get_or_create_share(session, almanach, user_id)
    return almanach


def list_almanachs(session: Session, user_id: str) -> Sequence[Almanach]:
    """Alle Sammlungen, auf die ein Profil Zugriff hat (eigene und freigegebene)."""
    almanach_ids = session.exec(
        select(AlmanachShare.almanach_id).where(AlmanachShare.plex_user_id == user_id)
    ).all()
    if not almanach_ids:
        return []
    stmt = select(Almanach).where(Almanach.id.in_(almanach_ids)).order_by(Almanach.name)
    return session.exec(stmt).all()


def get_almanach(session: Session, user_id: str, almanach_id: int) -> Optional[Almanach]:
    """Sammlung laden – nur wenn das Profil dafür freigegeben ist."""
    almanach = session.get(Almanach, almanach_id)
    if almanach is None or get_share(session, almanach_id, user_id) is None:
        return None
    return almanach



def rename_almanach(session: Session, almanach: Almanach, name: str) -> Almanach:
    """Umbenennen – die Playlist-Namen aller Profile ziehen mit.

    Der Playlist-Name ergibt sich immer aus der Vorlage. Ändert sich die
    Vorlage (oder der Name der Sammlung), wandern die vorhandenen Playlists
    beim nächsten Bau mit, statt unter dem alten Namen liegen zu bleiben.
    """
    almanach.name = name.strip() or almanach.name
    session.add(almanach)

    for share in list_shares(session, almanach.id):
        share.target_playlist_name = get_settings().almanach_playlist_name_for(
            share.plex_user_id, almanach.name
        )
        session.add(share)

    session.commit()
    session.refresh(almanach)
    return almanach


def delete_almanach(session: Session, almanach: Almanach) -> None:
    for entry in list_almanach_entries(session, almanach.id):
        session.delete(entry)
    for share in list_shares(session, almanach.id):
        session.delete(share)
    session.delete(almanach)
    session.commit()


# --- Freigaben: eine Playlist je Profil ----------------------------------


def get_share(
    session: Session, almanach_id: int, user_id: str
) -> Optional[AlmanachShare]:
    stmt = select(AlmanachShare).where(
        AlmanachShare.almanach_id == almanach_id,
        AlmanachShare.plex_user_id == user_id,
    )
    return session.exec(stmt).first()


def get_or_create_share(
    session: Session, almanach: Almanach, user_id: str
) -> AlmanachShare:
    share = get_share(session, almanach.id, user_id)
    if share is None:
        share = AlmanachShare(
            almanach_id=almanach.id,
            plex_user_id=user_id,
            target_playlist_name=get_settings().almanach_playlist_name_for(
                user_id, almanach.name
            ),
        )
        session.add(share)
        session.commit()
        session.refresh(share)
    elif not share.target_playlist_name:
        share.target_playlist_name = get_settings().almanach_playlist_name_for(
            user_id, almanach.name
        )
        session.add(share)
        session.commit()
        session.refresh(share)
    return share


def list_shares(session: Session, almanach_id: int) -> Sequence[AlmanachShare]:
    stmt = (
        select(AlmanachShare)
        .where(AlmanachShare.almanach_id == almanach_id)
        .order_by(AlmanachShare.plex_user_id)
    )
    return session.exec(stmt).all()


def share_user_ids(session: Session, almanach_id: int) -> set[str]:
    return {share.plex_user_id for share in list_shares(session, almanach_id)}


def remove_share(session: Session, almanach_id: int, user_id: str) -> Optional[AlmanachShare]:
    """Freigabe zurücknehmen; gibt die entfernte Zeile zurück (für die Playlist)."""
    share = get_share(session, almanach_id, user_id)
    if share is None:
        return None
    session.delete(share)
    session.commit()
    return share


def reset_cover_state(session: Session, almanach_id: int) -> None:
    """Nach einem Cover-Wechsel muss es auf jede Playlist neu übertragen werden."""
    for share in list_shares(session, almanach_id):
        share.cover_applied_at = None
        session.add(share)
    session.commit()


def shares_with_entries(session: Session) -> list[tuple[AlmanachShare, Almanach]]:
    """Alle Playlists, hinter denen eine gefüllte Sammlung steht (Scheduler)."""
    filled = set(session.exec(select(AlmanachEntry.almanach_id).distinct()).all())
    pairs: list[tuple[AlmanachShare, Almanach]] = []
    stmt = select(AlmanachShare).order_by(
        AlmanachShare.plex_user_id, AlmanachShare.almanach_id
    )
    for share in session.exec(stmt).all():
        if share.almanach_id not in filled:
            continue
        almanach = session.get(Almanach, share.almanach_id)
        if almanach is not None:
            pairs.append((share, almanach))
    return pairs


def list_almanach_entries(session: Session, almanach_id: int) -> Sequence[AlmanachEntry]:
    stmt = (
        select(AlmanachEntry)
        .where(AlmanachEntry.almanach_id == almanach_id)
        .order_by(AlmanachEntry.title)
    )
    return session.exec(stmt).all()


def almanach_keys(session: Session, almanach_id: int) -> set[str]:
    stmt = select(AlmanachEntry.plex_rating_key).where(
        AlmanachEntry.almanach_id == almanach_id
    )
    return {str(key) for key in session.exec(stmt).all()}


def add_to_almanach(
    session: Session,
    almanach: Almanach,
    rating_key: str,
    media_type: MediaType | str,
    title: str = "",
    year: Optional[int] = None,
) -> AlmanachEntry:
    rating_key = str(rating_key)
    existing = session.exec(
        select(AlmanachEntry).where(
            AlmanachEntry.almanach_id == almanach.id,
            AlmanachEntry.plex_rating_key == rating_key,
        )
    ).first()
    if existing:
        return existing

    entry = AlmanachEntry(
        almanach_id=almanach.id,
        plex_user_id=almanach.plex_user_id,
        plex_rating_key=rating_key,
        media_type=MediaType(media_type),
        title=title,
        year=year,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def remove_from_almanach(session: Session, almanach: Almanach, rating_key: str) -> bool:
    entry = session.exec(
        select(AlmanachEntry).where(
            AlmanachEntry.almanach_id == almanach.id,
            AlmanachEntry.plex_rating_key == str(rating_key),
        )
    ).first()
    if entry is None:
        return False
    session.delete(entry)
    session.commit()
    return True


def list_transition_clips(session: Session, user_id: str) -> Sequence[TransitionClip]:
    stmt = (
        select(TransitionClip)
        .where(TransitionClip.plex_user_id == user_id)
        .order_by(TransitionClip.day)
    )
    return session.exec(stmt).all()


def add_transition_clip(
    session: Session,
    user_id: str,
    day: date,
    period: tuple[Optional[date], Optional[date]],
    file_name: str,
    title: str,
    item_count: int,
) -> TransitionClip:
    clip = TransitionClip(
        plex_user_id=user_id,
        day=day,
        period_start=period[0],
        period_end=period[1],
        file_name=file_name,
        title=title,
        item_count=item_count,
    )
    session.add(clip)
    session.commit()
    session.refresh(clip)
    return clip


def drop_transition_clips(session: Session, user_id: str) -> list[str]:
    """Alle Clips eines Nutzers vergessen; gibt die Dateinamen zurück."""
    namen = []
    for clip in list_transition_clips(session, user_id):
        namen.append(clip.file_name)
        session.delete(clip)
    session.commit()
    return namen


def log_journey(
    session: Session,
    user_id: str,
    start: Optional[date],
    end: Optional[date],
    item_count: int,
    trigger: str = "manual",
    note: str = "",
    kind: str = "timemachine",
) -> JourneyLog:
    entry = JourneyLog(
        plex_user_id=user_id,
        date_start=start,
        date_end=end,
        item_count=item_count,
        trigger=trigger,
        note=note,
        kind=kind,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def list_journeys(session: Session, user_id: str, limit: int = 50) -> Sequence[JourneyLog]:
    stmt = (
        select(JourneyLog)
        .where(JourneyLog.plex_user_id == user_id)
        .order_by(JourneyLog.executed_at.desc())
        .limit(limit)
    )
    return session.exec(stmt).all()
