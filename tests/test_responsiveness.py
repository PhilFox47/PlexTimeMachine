"""Ein langsames Plex darf die Weboberfläche nicht anhalten."""

from __future__ import annotations

import asyncio
import time

import httpx

from app.plex_client import HomeUser, set_gateway


class LangsamesGateway:
    """Doppelgänger, dessen Plex-Zugriff spürbar dauert."""

    def __init__(self, verzoegerung: float = 1.0):
        self.verzoegerung = verzoegerung

    def home_users(self):
        time.sleep(self.verzoegerung)  # blockierend, wie plexapi es ist
        return [HomeUser(id="Alex", title="Alex", is_admin=True)]

    def connect_as(self, user_id):
        time.sleep(self.verzoegerung)
        raise RuntimeError("nicht benötigt")


def _run(coro):
    return asyncio.run(coro)


def test_slow_plex_does_not_block_other_requests():
    """Während ein Seitenaufruf auf Plex wartet, muss der Rest weiterlaufen.

    Vorher liefen die plexapi-Aufrufe im Event-Loop: ein nicht erreichbarer
    Server legte damit den kompletten Webserver lahm, nicht nur die Seite.
    """
    from app.main import app

    set_gateway(LangsamesGateway(verzoegerung=1.0))

    async def szenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as zweiter:
                # Gemessen wird die Zeit *bis* die schnelle Antwort da ist. Läge
                # der Plex-Aufruf im Event-Loop, käme sie erst nach dessen Ende.
                start = time.perf_counter()
                langsam = asyncio.create_task(client.get("/blacklist"))
                await asyncio.sleep(0.05)
                antwort = await zweiter.get("/healthz")
                bis_zur_antwort = time.perf_counter() - start

                langsame_antwort = await langsam
                return (
                    antwort.status_code,
                    bis_zur_antwort,
                    langsame_antwort.status_code,
                    time.perf_counter() - start,
                )

    try:
        status, bis_zur_antwort, langsam_status, gesamt = _run(szenario())
    finally:
        set_gateway(None)

    assert status == 200 and langsam_status == 200
    assert bis_zur_antwort < 0.5, (
        f"/healthz kam erst nach {bis_zur_antwort:.2f}s – der Event-Loop war blockiert"
    )
    assert gesamt >= 1.0  # der langsame Aufruf hat tatsächlich gewartet


def test_pages_still_render_when_plex_is_unreachable():
    """Ohne Plex zeigt die Seite einen Hinweis statt gar nichts."""
    from app.main import app
    from app.plex_client import PlexUnavailable

    class TotesGateway:
        def home_users(self):
            raise PlexUnavailable("Plex nicht erreichbar unter http://beispiel:32400")

        def connect_as(self, user_id):
            raise PlexUnavailable("Plex nicht erreichbar")

    set_gateway(TotesGateway())

    async def szenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return {pfad: await client.get(pfad) for pfad in ("/", "/almanach", "/logbook")}

    try:
        antworten = _run(szenario())
    finally:
        set_gateway(None)

    for pfad, antwort in antworten.items():
        assert antwort.status_code == 200, pfad
        assert "Keine Verbindung zu Plex" in antwort.text, pfad


def test_unwritable_data_directory_fails_with_a_clear_message(tmp_path, monkeypatch, caplog):
    """Ein nicht beschreibbares Volume soll benannt werden, nicht nur crashen.

    Der Schreibversuch wird hier erzwungen abgewiesen – die Testsuite läuft je
    nach Umgebung als root und käme sonst an jeder Dateirechte-Sperre vorbei.
    """
    import pathlib as _pathlib

    import pytest

    from app import config
    from app.main import _check_data_dir

    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{tmp_path}/daten/ptm.db")
    config.get_settings.cache_clear()

    def kein_schreiben(self, data):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(_pathlib.Path, "write_bytes", kein_schreiben)

    try:
        with caplog.at_level("ERROR"):
            with pytest.raises(OSError):
                _check_data_dir()
        assert "nicht beschreibbar" in caplog.text
        assert "chown" in caplog.text
    finally:
        config.get_settings.cache_clear()


def test_writable_data_directory_passes(tmp_path, monkeypatch):
    from app import config
    from app.main import _check_data_dir

    monkeypatch.setenv("PTM_DATABASE_URL", f"sqlite:///{tmp_path}/unterordner/ptm.db")
    config.get_settings.cache_clear()
    try:
        _check_data_dir()
        assert (tmp_path / "unterordner").is_dir()
    finally:
        config.get_settings.cache_clear()
