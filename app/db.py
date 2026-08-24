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


class AlmanachEntry(SQLModel, table=True):
    """Eine bewusst ausgewählte Serie/ein Film für den Almanach – pro Nutzer."""

    __tablename__ = "almanach_entry"

    id: Optional[int] = Field(default=None, primary_key=True)
    plex_user_id: str = Field(index=True)
    plex_rating_key: str = Field(index=True)
    media_type: MediaType = MediaType.movie
    title: str = ""
    year: Optional[int] = None
    added_at: datetime = Field(default_factory=utcnow)

    @property
    def is_show(self) -> bool:
        return MediaType(self.media_type) is MediaType.show


class AlmanachState(SQLModel, table=True):
    """Zustand der Almanach-Playlist eines Nutzers (eigener Lebenszyklus)."""

    __tablename__ = "almanach_state"

    plex_user_id: str = Field(primary_key=True)
    target_playlist_name: str = ""
    last_synced_at: Optional[datetime] = None
    last_item_count: int = 0


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


def init_db() -> None:
    engine = get_engine()
    _add_missing_columns(engine)
    SQLModel.metadata.create_all(engine)


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


def get_or_create_almanach_state(session: Session, user_id: str) -> AlmanachState:
    state = session.get(AlmanachState, user_id)
    if state is None:
        state = AlmanachState(
            plex_user_id=user_id,
            target_playlist_name=get_settings().almanach_playlist_name_for(user_id),
        )
        session.add(state)
        session.commit()
        session.refresh(state)
    elif not state.target_playlist_name:
        state.target_playlist_name = get_settings().almanach_playlist_name_for(user_id)
        session.add(state)
        session.commit()
        session.refresh(state)
    return state


def list_almanach(session: Session, user_id: str) -> Sequence[AlmanachEntry]:
    stmt = (
        select(AlmanachEntry)
        .where(AlmanachEntry.plex_user_id == user_id)
        .order_by(AlmanachEntry.title)
    )
    return session.exec(stmt).all()


def almanach_keys(session: Session, user_id: str) -> set[str]:
    stmt = select(AlmanachEntry.plex_rating_key).where(
        AlmanachEntry.plex_user_id == user_id
    )
    return {str(key) for key in session.exec(stmt).all()}


def add_to_almanach(
    session: Session,
    user_id: str,
    rating_key: str,
    media_type: MediaType | str,
    title: str = "",
    year: Optional[int] = None,
) -> AlmanachEntry:
    rating_key = str(rating_key)
    existing = session.exec(
        select(AlmanachEntry).where(
            AlmanachEntry.plex_user_id == user_id,
            AlmanachEntry.plex_rating_key == rating_key,
        )
    ).first()
    if existing:
        return existing

    entry = AlmanachEntry(
        plex_user_id=user_id,
        plex_rating_key=rating_key,
        media_type=MediaType(media_type),
        title=title,
        year=year,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def remove_from_almanach(session: Session, user_id: str, rating_key: str) -> bool:
    entry = session.exec(
        select(AlmanachEntry).where(
            AlmanachEntry.plex_user_id == user_id,
            AlmanachEntry.plex_rating_key == str(rating_key),
        )
    ).first()
    if entry is None:
        return False
    session.delete(entry)
    session.commit()
    return True


def users_with_almanach(session: Session) -> list[str]:
    stmt = select(AlmanachEntry.plex_user_id).distinct()
    return list(session.exec(stmt).all())


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
