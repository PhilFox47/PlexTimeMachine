"""SQLite-Persistenz: UserState, BlacklistEntry, JourneyLog."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Iterator, Optional, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.config import get_settings


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
    """Eine benannte Sammlung – ein Nutzer kann beliebig viele davon führen."""

    __tablename__ = "almanach"

    id: Optional[int] = Field(default=None, primary_key=True)
    plex_user_id: str = Field(index=True)
    name: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    target_playlist_name: str = ""
    last_synced_at: Optional[datetime] = None
    last_item_count: int = 0


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
        connect_args = {"check_same_thread": False}
        kwargs = {}
        if url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, echo=False, connect_args=connect_args, **kwargs)
    return _engine


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
            state = legacy_state.get(user_id)
            almanach.target_playlist_name = (
                state[1]
                if state and state[1]
                else get_settings().almanach_playlist_name_for(user_id, almanach.name)
            )
            if state:
                almanach.last_item_count = state[3] or 0
            session.add(almanach)
            session.commit()
            session.refresh(almanach)

            for entry in entries:
                entry.almanach_id = almanach.id
                session.add(entry)
            session.commit()
            migrated += len(entries)

    return migrated


def init_db() -> None:
    engine = get_engine()
    _add_missing_columns(engine)
    SQLModel.metadata.create_all(engine)
    _migrate_legacy_almanach(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


# ---------------------------------------------------------------------------
# Repository-Funktionen
# ---------------------------------------------------------------------------


def get_or_create_user_state(session: Session, user_id: str) -> UserState:
    state = session.get(UserState, user_id)
    if state is None:
        state = UserState(
            plex_user_id=user_id,
            target_playlist_name=get_settings().playlist_name_for(user_id),
        )
        session.add(state)
        session.commit()
        session.refresh(state)
    elif not state.target_playlist_name:
        state.target_playlist_name = get_settings().playlist_name_for(user_id)
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
    almanach = Almanach(
        plex_user_id=user_id,
        name=name.strip() or "Ohne Namen",
    )
    almanach.target_playlist_name = get_settings().almanach_playlist_name_for(
        user_id, almanach.name
    )
    session.add(almanach)
    session.commit()
    session.refresh(almanach)
    return almanach


def list_almanachs(session: Session, user_id: str) -> Sequence[Almanach]:
    stmt = (
        select(Almanach)
        .where(Almanach.plex_user_id == user_id)
        .order_by(Almanach.name)
    )
    return session.exec(stmt).all()


def get_almanach(session: Session, user_id: str, almanach_id: int) -> Optional[Almanach]:
    """Almanach laden – nur wenn er dem angegebenen Nutzer gehört."""
    almanach = session.get(Almanach, almanach_id)
    if almanach is None or almanach.plex_user_id != user_id:
        return None
    return almanach


def rename_almanach(session: Session, almanach: Almanach, name: str) -> Almanach:
    """Umbenennen – der Playlist-Name zieht mit, sofern er dem Muster folgt."""
    settings = get_settings()
    old_default = settings.almanach_playlist_name_for(
        almanach.plex_user_id, almanach.name
    )
    almanach.name = name.strip() or almanach.name
    if not almanach.target_playlist_name or almanach.target_playlist_name == old_default:
        almanach.target_playlist_name = settings.almanach_playlist_name_for(
            almanach.plex_user_id, almanach.name
        )
    session.add(almanach)
    session.commit()
    session.refresh(almanach)
    return almanach


def delete_almanach(session: Session, almanach: Almanach) -> None:
    for entry in list_almanach_entries(session, almanach.id):
        session.delete(entry)
    session.delete(almanach)
    session.commit()


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


def almanachs_with_entries(session: Session) -> list[Almanach]:
    """Alle Almanachs, die mindestens einen Eintrag haben (für den Scheduler)."""
    filled = set(session.exec(select(AlmanachEntry.almanach_id).distinct()).all())
    stmt = select(Almanach).order_by(Almanach.plex_user_id, Almanach.name)
    return [a for a in session.exec(stmt).all() if a.id in filled]


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
