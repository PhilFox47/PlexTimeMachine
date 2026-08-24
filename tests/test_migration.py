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
        db.add_to_almanach(session, "Alex", "42", "show", "Star Wars: Andor")
        assert db.almanach_keys(session, "Alex") == {"42"}

    db.reset_engine()
    config.get_settings.cache_clear()


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{tmp_path/'neu.db'}")
    config.get_settings.cache_clear()
    db.reset_engine()

    db.init_db()
    db.init_db()  # zweiter Start darf nichts kaputt machen

    with Session(db.get_engine()) as session:
        assert db.list_almanach(session, "Alex") == []

    db.reset_engine()
    config.get_settings.cache_clear()
