"""Bestehende Datenbanken müssen ein Update überleben."""

from __future__ import annotations

import sqlite3

from sqlmodel import Session, select

from app import config, db


def test_existing_database_gains_new_columns(tmp_path, monkeypatch):
    """Eine v1-Datenbank ohne 'kind' behält ihre Daten und bekommt die Spalte."""
    path = tmp_path / "alt.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE journey_log (
            id INTEGER PRIMARY KEY,
            plex_user_id VARCHAR NOT NULL,
            date_start DATE,
            date_end DATE,
            executed_at DATETIME NOT NULL,
            item_count INTEGER NOT NULL,
            trigger VARCHAR NOT NULL,
            note VARCHAR NOT NULL
        );
        INSERT INTO journey_log
            (plex_user_id, date_start, date_end, executed_at, item_count, trigger, note)
        VALUES ('Alex', '1985-01-01', '1985-12-31', '2026-01-01 10:00:00', 5, 'manual', '');
        """
    )
    old.commit()
    old.close()

    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{path}")
    config.get_settings.cache_clear()
    db.reset_engine()

    db.init_db()

    with Session(db.get_engine()) as session:
        journeys = session.exec(select(db.JourneyLog)).all()
        assert len(journeys) == 1  # Altbestand ist erhalten
        assert journeys[0].item_count == 5
        assert journeys[0].kind == "timemachine"  # neue Spalte mit Default

        # Die neuen Tabellen sind ebenfalls da und benutzbar.
        almanach = db.create_almanach(session, "Alex", "Star Wars")
        db.add_to_almanach(session, almanach, "42", "show", "Star Wars: Andor")
        assert db.almanach_keys(session, almanach.id) == {"42"}

    db.reset_engine()
    config.get_settings.cache_clear()


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{tmp_path/'neu.db'}")
    config.get_settings.cache_clear()
    db.reset_engine()

    db.init_db()
    db.init_db()  # zweiter Start darf nichts kaputt machen

    with Session(db.get_engine()) as session:
        assert db.list_almanachs(session, "Alex") == []

    db.reset_engine()
    config.get_settings.cache_clear()


def test_legacy_almanach_becomes_a_named_collection(tmp_path, monkeypatch):
    """Der namenlose Almanach aus der Vorversion wird zu »Mein Almanach«."""
    path = tmp_path / "v3.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE almanach_entry (
            id INTEGER PRIMARY KEY,
            plex_user_id VARCHAR NOT NULL,
            plex_rating_key VARCHAR NOT NULL,
            media_type VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            year INTEGER,
            added_at DATETIME NOT NULL
        );
        CREATE TABLE almanach_state (
            plex_user_id VARCHAR PRIMARY KEY,
            target_playlist_name VARCHAR NOT NULL,
            last_synced_at DATETIME,
            last_item_count INTEGER NOT NULL
        );
        INSERT INTO almanach_entry
            (plex_user_id, plex_rating_key, media_type, title, year, added_at)
        VALUES ('Alex', '100', 'show', 'Knight Rider', 1982, '2026-01-01 10:00:00'),
               ('Alex', '1', 'movie', 'Zurück in die Zukunft', 1985, '2026-01-01 10:05:00'),
               ('Nina', '2', 'movie', 'Brazil', 1985, '2026-01-01 10:06:00');
        INSERT INTO almanach_state
            (plex_user_id, target_playlist_name, last_synced_at, last_item_count)
        VALUES ('Alex', 'Plex Almanach – Alex', '2026-01-02 09:00:00', 12);
        """
    )
    old.commit()
    old.close()

    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{path}")
    config.get_settings.cache_clear()
    db.reset_engine()

    db.init_db()

    with Session(db.get_engine()) as session:
        alex = db.list_almanachs(session, "Alex")
        assert [a.name for a in alex] == ["Mein Almanach"]
        # Der bestehende Playlist-Name bleibt erhalten, damit die vorhandene
        # Plex-Playlist weiterverwendet wird statt eine zweite anzulegen.
        assert alex[0].target_playlist_name == "Plex Almanach – Alex"
        assert alex[0].last_item_count == 12
        assert db.almanach_keys(session, alex[0].id) == {"100", "1"}

        nina = db.list_almanachs(session, "Nina")
        assert len(nina) == 1 and db.almanach_keys(session, nina[0].id) == {"2"}

        # Ein zweiter Start darf keinen weiteren Almanach erzeugen.
        db.init_db()
        assert len(db.list_almanachs(session, "Alex")) == 1

    db.reset_engine()
    config.get_settings.cache_clear()
