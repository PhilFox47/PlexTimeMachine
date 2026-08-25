"""Die Datenbank muss auch während eines langen Syncs bedienbar bleiben."""

from __future__ import annotations

import threading
import time

from sqlmodel import Session, text

from app import config, db


def _neue_datenbank(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{tmp_path/'ptm.db'}")
    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()


def test_sqlite_runs_in_wal_mode_with_a_generous_timeout(tmp_path, monkeypatch):
    """Ohne WAL sperrt jeder Schreibvorgang die komplette Datei."""
    _neue_datenbank(tmp_path, monkeypatch)

    with Session(db.get_engine()) as session:
        modus = session.exec(text("PRAGMA journal_mode")).one()[0]
        wartezeit = session.exec(text("PRAGMA busy_timeout")).one()[0]

    assert str(modus).lower() == "wal"
    assert wartezeit == db.LOCK_TIMEOUT_SECONDS * 1000

    db.reset_engine()
    config.get_settings.cache_clear()


def test_writing_works_while_a_long_sync_holds_the_database(tmp_path, monkeypatch):
    """Ein Almanach anlegen darf nicht an einem laufenden Sync scheitern.

    Vorher brach genau das nach fünf Sekunden mit "database is locked" ab –
    in der Oberfläche ein Internal Server Error.
    """
    _neue_datenbank(tmp_path, monkeypatch)
    laeuft = threading.Event()

    def langer_sync():
        with Session(db.get_engine()) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            laeuft.set()
            time.sleep(1.5)  # in echt: Plex-Abfragen für eine große Playlist
            session.exec(text("COMMIT"))

    thread = threading.Thread(target=langer_sync)
    thread.start()
    assert laeuft.wait(timeout=5)

    start = time.perf_counter()
    with Session(db.get_engine(), expire_on_commit=False) as session:
        almanach = db.create_almanach(session, "Leo", "Testliste")
        dauer = time.perf_counter() - start
        assert almanach.id is not None
        assert db.get_share(session, almanach.id, "Leo") is not None
    thread.join()

    assert dauer < db.LOCK_TIMEOUT_SECONDS  # es wurde gewartet, nicht abgebrochen

    db.reset_engine()
    config.get_settings.cache_clear()


def test_reading_is_not_blocked_by_a_running_write(tmp_path, monkeypatch):
    """Mit WAL darf Lesen währenddessen sofort durchgehen."""
    _neue_datenbank(tmp_path, monkeypatch)
    with Session(db.get_engine()) as session:
        db.create_almanach(session, "Phil", "Marvel")

    laeuft = threading.Event()

    def schreibt_lange():
        with Session(db.get_engine()) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            laeuft.set()
            time.sleep(1.5)
            session.exec(text("COMMIT"))

    thread = threading.Thread(target=schreibt_lange)
    thread.start()
    assert laeuft.wait(timeout=5)

    start = time.perf_counter()
    with Session(db.get_engine()) as session:
        sammlungen = db.list_almanachs(session, "Phil")
    dauer = time.perf_counter() - start
    thread.join()

    assert [a.name for a in sammlungen] == ["Marvel"]
    assert dauer < 0.5, f"Lesen wurde blockiert ({dauer:.1f}s)"

    db.reset_engine()
    config.get_settings.cache_clear()
