"""Tests für Cover-Bilder: Prüfung, Ablage und Übertragung an Plex."""

from __future__ import annotations

import pytest

from app import covers, db
from app.almanach import sync_almanach
from app.sync_engine import apply_cover_after_sync, apply_playlist, clear_cover, push_cover
from tests.conftest import make_png

WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
GIF = b"GIF89a" + b"\x00" * 10


@pytest.fixture
def almanach(session):
    return db.create_almanach(session, "Alex", "Star Wars")


# ---------------------------------------------------------------------------
# Prüfung und Ablage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [(make_png(), ".png"), (b"\xff\xd8\xff\xe0rest", ".jpg"), (GIF, ".gif"), (WEBP, ".webp")],
)
def test_detect_extension_reads_the_content(data, expected):
    assert covers.detect_extension(data) == expected


def test_detect_extension_rejects_other_content():
    assert covers.detect_extension(b"%PDF-1.7 kein Bild") is None
    assert covers.detect_extension(b"") is None


def test_store_keeps_the_file_and_returns_its_name(png_image):
    filename = covers.store("almanach-7", png_image)

    assert filename == "almanach-7.png"
    assert covers.path_for(filename).read_bytes() == png_image


def test_store_refuses_non_images():
    with pytest.raises(covers.CoverError, match="JPEG"):
        covers.store("almanach-7", b"<html>kein Bild</html>")


def test_store_refuses_empty_files():
    with pytest.raises(covers.CoverError, match="leer"):
        covers.store("almanach-7", b"")


def test_store_refuses_oversized_files(monkeypatch, png_image):
    from app import config

    monkeypatch.setenv("PTM_COVER_MAX_BYTES", "10")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(covers.CoverError, match="größer"):
            covers.store("almanach-7", png_image)
    finally:
        config.get_settings.cache_clear()


def test_store_replaces_an_earlier_cover_of_another_type(png_image):
    covers.store("almanach-7", b"\xff\xd8\xff\xe0altes JPEG")
    covers.store("almanach-7", png_image)

    files = sorted(p.name for p in covers.cover_dir().glob("almanach-7.*"))
    assert files == ["almanach-7.png"]  # keine Karteileiche mit alter Endung


def test_path_for_handles_missing_files():
    assert covers.path_for(None) is None
    assert covers.path_for("gibtsnicht.png") is None


def test_stems_are_stable_and_distinct():
    assert covers.almanach_stem(3) == "almanach-3"
    assert covers.timemachine_stem("Alex") == covers.timemachine_stem("Alex")
    assert covers.timemachine_stem("Alex") != covers.timemachine_stem("Nina")


# ---------------------------------------------------------------------------
# Übertragung an Plex
# ---------------------------------------------------------------------------


def test_cover_is_uploaded_when_the_playlist_is_created(gateway, png_image):
    from tests.conftest import FakeMovie

    filename = covers.store("almanach-1", png_image)
    outcome = apply_playlist(gateway.server, "Testliste", [FakeMovie(1, "Film", "1985-01-01")])

    assert apply_cover_after_sync(outcome, filename, already_applied=False) is True
    assert gateway.server.playlists()[0].posters == [png_image]


def test_cover_is_not_uploaded_again_on_every_sync(gateway, png_image):
    from tests.conftest import FakeMovie

    filename = covers.store("almanach-1", png_image)
    items = [FakeMovie(1, "Film", "1985-01-01")]
    apply_playlist(gateway.server, "Testliste", items)

    # Playlist besteht schon -> zweiter Lauf lässt das Poster in Ruhe.
    outcome = apply_playlist(gateway.server, "Testliste", items)
    assert apply_cover_after_sync(outcome, filename, already_applied=True) is False
    assert gateway.server.playlists()[0].posters == []


def test_cover_returns_after_the_playlist_was_rebuilt(gateway, png_image):
    """Wird die Playlist neu angelegt, muss das Poster erneut hoch."""
    from tests.conftest import FakeMovie

    filename = covers.store("almanach-1", png_image)
    items = [FakeMovie(1, "Film", "1985-01-01")]
    apply_playlist(gateway.server, "Testliste", items)
    apply_playlist(gateway.server, "Testliste", [])  # alles gesehen -> Playlist weg

    outcome = apply_playlist(gateway.server, "Testliste", items)

    assert outcome.created is True
    assert apply_cover_after_sync(outcome, filename, already_applied=True) is True
    assert gateway.server.playlists()[0].posters == [png_image]


def test_apply_cover_without_a_cover_does_nothing(gateway):
    from tests.conftest import FakeMovie

    outcome = apply_playlist(gateway.server, "Testliste", [FakeMovie(1, "F", "1985-01-01")])

    assert apply_cover_after_sync(outcome, None, already_applied=False) is False


def test_sync_uploads_the_cover_of_the_collection(session, gateway, almanach, png_image):
    almanach.cover_path = covers.store(covers.almanach_stem(almanach.id), png_image)
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")

    result = sync_almanach(session, almanach, gateway=gateway)

    assert result.ok
    assert gateway.server.playlists()[0].posters == [png_image]
    assert almanach.cover_applied_at is not None


def test_push_cover_reaches_an_existing_playlist(session, gateway, almanach, png_image):
    filename = covers.store(covers.almanach_stem(almanach.id), png_image)
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    sync_almanach(session, almanach, gateway=gateway)

    assert push_cover(gateway, "Alex", almanach.target_playlist_name, filename) is True
    assert gateway.server.playlists()[0].posters[-1] == png_image


def test_push_cover_waits_when_there_is_no_playlist_yet(gateway, png_image):
    filename = covers.store("almanach-9", png_image)

    assert push_cover(gateway, "Alex", "Noch nicht da", filename) is False


def test_clear_cover_removes_the_poster(session, gateway, almanach, png_image):
    almanach.cover_path = covers.store(covers.almanach_stem(almanach.id), png_image)
    db.add_to_almanach(session, almanach, "2", "movie", "Brazil")
    sync_almanach(session, almanach, gateway=gateway)

    assert clear_cover(gateway, "Alex", almanach.target_playlist_name) is True
    playlist = gateway.server.playlists()[0]
    assert playlist.posters == [] and playlist.poster_deleted is True
