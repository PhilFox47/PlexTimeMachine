"""Tests für die Übergangsclips: Rendern, Tagesaufteilung, Einfädeln."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app import db, transition_build, transitions
from app.transitions import ClipItem, ClipSpec


def ffmpeg_pfad() -> str | None:
    """Für die Tests reicht der Binärling aus imageio-ffmpeg; im Image ist es
    das Systempaket."""
    try:
        import imageio_ffmpeg
    except ImportError:  # pragma: no cover - nur ohne Entwicklungspaket
        return shutil.which("ffmpeg")
    return imageio_ffmpeg.get_ffmpeg_exe()


# ---------------------------------------------------------------------------
# Layout und Länge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("anzahl", [1, 2, 3, 5, 7, 10])
def test_the_programme_fits_on_one_screen(anzahl):
    """Egal ob ein Titel oder zehn: alles steht auf einer Tafel, ohne Überlauf."""
    stage = transitions.Stage(360)
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", f"Film {i}", f"Film {i}", year=1985, slot="20:15")
                     for i in range(anzahl)])

    board = transitions.Board(stage, spec)

    assert len(board.zeilen) == anzahl
    assert board.positionen[0][1] >= stage.liste_oben - 1
    unten = board.positionen[-1][1] + board.hoehe
    assert unten <= stage.liste_unten + 1
    # Die Zeilen überlappen sich nicht.
    abstaende = [b[1] - a[1] for a, b in zip(board.positionen, board.positionen[1:])]
    assert all(abstand >= board.hoehe for abstand in abstaende)


def test_more_than_ten_titles_are_summarised():
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", f"Film {i}", f"Film {i}", year=1985) for i in range(14)])

    assert len(spec.shown) == 10 and spec.extra == 4


def test_duration_grows_with_the_number_of_titles():
    assert transitions.clip_duration(1) < transitions.clip_duration(10)
    assert transitions.clip_duration(10) == pytest.approx(11.1, abs=0.1)


# ---------------------------------------------------------------------------
# Tage bilden und einfädeln
# ---------------------------------------------------------------------------


def _items(gateway):
    from app.sync_engine import collect_items

    return collect_items(gateway, gateway.server, date(1985, 1, 1), date(1985, 12, 31))


def test_days_are_grouped_and_empty_days_never_appear(gateway):
    tage = transition_build.group_by_day(_items(gateway))

    assert [t for t, _ in tage] == [date(1985, 2, 22), date(1985, 7, 3), date(1985, 9, 20)]
    assert [len(i) for _, i in tage] == [2, 1, 2]  # 20.09. hat zwei Folgen


def test_interleave_puts_the_clip_in_front_of_its_day(gateway):
    items = _items(gateway)
    clips = {date(1985, 2, 22): "CLIP-A", date(1985, 9, 20): "CLIP-B"}

    reihe = transition_build.interleave(items, clips)

    titel = [x if isinstance(x, str) else x.title for x in reihe]
    assert titel == [
        "CLIP-A", "Showdown", "Brazil",       # Serie um 10:00, Film um 20:15
        "Zurück in die Zukunft",              # 03.07. ohne Clip
        "CLIP-B", "Pilot", "Folge 2",
    ]


def test_clip_names_are_safe_for_the_file_system():
    titel = transition_build.clip_title("Zeitreisende Ente", date(2026, 8, 25))
    name = transition_build.clip_file_name("Zeitreisende Ente", date(2026, 8, 25))

    assert titel == "Time Machine - Tuesday 25.08.2026 - Zeitreisende Ente"
    assert name.endswith(".mp4")
    assert not set(name) & set('<>:"/\\|?*')


# ---------------------------------------------------------------------------
# Nur bei neuem Zeitraum neu erzeugen
# ---------------------------------------------------------------------------


def test_rebuild_is_needed_without_clips(session):
    assert transition_build.needs_rebuild(session, "Alex", (date(1985, 1, 1), date(1985, 12, 31)))


def test_no_rebuild_within_the_same_period(session):
    periode = (date(1985, 1, 1), date(1985, 12, 31))
    db.add_transition_clip(session, "Alex", date(1985, 2, 22), periode, "a.mp4", "A", 2)

    assert transition_build.needs_rebuild(session, "Alex", periode) is False


def test_rebuild_after_switching_the_week(session):
    db.add_transition_clip(session, "Alex", date(1985, 2, 22),
                           (date(1985, 1, 1), date(1985, 12, 31)), "a.mp4", "A", 2)

    assert transition_build.needs_rebuild(session, "Alex", (date(1986, 1, 1), date(1986, 12, 31)))


def test_discarding_removes_files_and_records(session, tmp_path, monkeypatch):
    monkeypatch.setenv("PTM_TRANSITION_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()
    (tmp_path / "alt.mp4").write_bytes(b"x")
    db.add_transition_clip(session, "Alex", date(1985, 2, 22), (None, None), "alt.mp4", "A", 1)

    entfernt = transition_build.discard_clips(session, "Alex")

    assert entfernt == 1
    assert not (tmp_path / "alt.mp4").exists()
    assert db.list_transition_clips(session, "Alex") == []
    config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Poster: für Episoden das Staffelposter
# ---------------------------------------------------------------------------


def test_episodes_use_the_season_poster(gateway, monkeypatch):
    geholt = []

    def fake_fetch(server, pfad, timeout=15):
        geholt.append(pfad)
        return b"BILD" if pfad else None

    monkeypatch.setattr(transition_build, "fetch_image", fake_fetch)
    episode = _items(gateway)[-1]
    episode.parent_thumb = "/library/metadata/101/thumb/1"

    clip_item = transition_build.to_clip_item(gateway.server, episode)

    assert geholt[0] == "/library/metadata/101/thumb/1"   # Staffel, nicht Serie
    assert clip_item.kind == "episode" and clip_item.poster == b"BILD"
    assert clip_item.show == "Knight Rider" and clip_item.season == 1


def test_movies_use_their_own_poster(gateway, monkeypatch):
    monkeypatch.setattr(transition_build, "fetch_image", lambda s, p, timeout=15: b"P" if p else None)
    film = [i for i in _items(gateway) if not i.is_episode][0]

    clip_item = transition_build.to_clip_item(gateway.server, film)

    assert clip_item.kind == "movie" and clip_item.year == 1985


# ---------------------------------------------------------------------------
# Wirklich rendern
# ---------------------------------------------------------------------------


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_render_produces_a_playable_clip(tmp_path):
    """Ein echter Durchlauf – klein gehalten, damit es schnell bleibt."""
    spec = ClipSpec(
        "MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
        [ClipItem("episode", "Chrono Patrol", "Der lange Freitag", 3, 5),
         ClipItem("movie", "Die Rückfahrt", "Die Rückfahrt", year=1985)],
    )
    ziel = tmp_path / "clip.mp4"

    transitions.render_clip(spec, ziel, height=360, ffmpeg=ffmpeg_pfad())

    assert ziel.exists() and ziel.stat().st_size > 10_000
    beschreibung = subprocess.run(
        [ffmpeg_pfad(), "-hide_banner", "-i", str(ziel)], capture_output=True, text=True
    ).stderr
    assert "Video: h264" in beschreibung        # von Plex-Clients gut verdaut
    assert "Audio: aac" in beschreibung         # stille Tonspur, sonst stolpern manche


def test_missing_ffmpeg_is_reported_clearly(tmp_path):
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", "Film", "Film", year=1985)])

    with pytest.raises(transitions.RenderError, match="FFmpeg nicht gefunden"):
        transitions.render_clip(spec, tmp_path / "x.mp4", height=360,
                                ffmpeg="/gibt/es/nicht/ffmpeg")


def test_broken_poster_data_falls_back_to_a_placeholder():
    """Ein kaputtes Bild darf den ganzen Clip nicht scheitern lassen."""
    item = ClipItem("movie", "Film", "Film", year=1985, poster=b"kein Bild")

    bild = transitions._poster(item, (120, 180))

    assert bild.size == (120, 180)


# ---------------------------------------------------------------------------
# Durchstich: erzeugen, einlesen, in die Playlist
# ---------------------------------------------------------------------------


@pytest.fixture
def uebergaenge_an(tmp_path, monkeypatch):
    """Übergänge einschalten, klein und schnell rendern."""
    from app import config

    monkeypatch.setenv("PTM_TRANSITIONS_ENABLED", "true")
    monkeypatch.setenv("PTM_TRANSITION_DIR", str(tmp_path / "clips"))
    monkeypatch.setenv("PTM_TRANSITION_LIBRARY", "Zeitreise-Übergänge")
    monkeypatch.setenv("PTM_TRANSITION_HEIGHT", "360")
    monkeypatch.setenv("PTM_TRANSITION_MAX_CLIPS", "2")
    monkeypatch.setenv("PTM_TRANSITION_USER", "Alex")
    monkeypatch.setenv("PTM_TRANSITION_SCAN_DELAY_SECONDS", "0")
    if ffmpeg_pfad():
        monkeypatch.setenv("PTM_FFMPEG_BINARY", ffmpeg_pfad())
    config.get_settings.cache_clear()
    yield tmp_path / "clips"
    config.get_settings.cache_clear()


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_clips_are_built_scanned_and_woven_into_the_playlist(
    session, gateway, uebergaenge_an
):
    from app.sync_engine import sync_user

    gateway.server.mit_uebergaengen(uebergaenge_an)
    periode = (date(1985, 1, 1), date(1985, 12, 31))
    db.set_period(session, "Alex", *periode)
    items = _items(gateway)

    titel = transition_build.build_clips(session, "Alex", items, periode, gateway)

    # Zwei Tage (Obergrenze), Dateien liegen im Ordner
    assert len(titel) == 2
    assert sorted(p.name for p in uebergaenge_an.glob("*.mp4")) == [
        "Time Machine - Friday 22.02.1985 - Alex.mp4",
        "Time Machine - Wednesday 03.07.1985 - Alex.mp4",
    ]
    assert len(db.list_transition_clips(session, "Alex")) == 2

    # Vor dem Scan kennt Plex sie nicht ...
    assert transition_build.clips_for_playlist(session, "Alex", gateway.server) == {}

    transition_build.rescan_library(gateway.server)
    clips = transition_build.clips_for_playlist(session, "Alex", gateway.server)
    assert set(clips) == {date(1985, 2, 22), date(1985, 7, 3)}

    ergebnis = sync_user(session, "Alex", gateway=gateway)

    assert ergebnis.ok
    playlist = gateway.server.playlists()[0]
    namen = [x.title for x in playlist.items()]
    assert namen[0].startswith("Time Machine - Friday 22.02.1985")
    assert namen[1:3] == ["Showdown", "Brazil"]
    assert namen[3].startswith("Time Machine - Wednesday 03.07.1985")
    assert namen[4] == "Zurück in die Zukunft"
    # Der 20.09. bekam wegen der Obergrenze keinen Clip – seine Titel bleiben trotzdem
    assert namen[5:] == ["Pilot", "Folge 2"]


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_switching_the_week_replaces_the_old_clips(session, gateway, uebergaenge_an):
    gateway.server.mit_uebergaengen(uebergaenge_an)
    alt = (date(1985, 1, 1), date(1985, 12, 31))
    transition_build.build_clips(session, "Alex", _items(gateway), alt, gateway)
    alte_dateien = {p.name for p in uebergaenge_an.glob("*.mp4")}

    from app.sync_engine import collect_items

    neu = (date(1999, 1, 1), date(1999, 12, 31))
    items_neu = collect_items(gateway, gateway.server, *neu)
    transition_build.build_clips(session, "Alex", items_neu, neu, gateway)

    jetzt = {p.name for p in uebergaenge_an.glob("*.mp4")}
    assert jetzt == {"Time Machine - Wednesday 31.03.1999 - Alex.mp4"}
    assert not (jetzt & alte_dateien)         # alte Dateien sind weg
    assert [c.day for c in db.list_transition_clips(session, "Alex")] == [date(1999, 3, 31)]


def test_playlist_stays_intact_when_no_clips_exist(session, gateway, uebergaenge_an):
    """Ohne erzeugte Clips ändert sich an der Playlist nichts."""
    from app.sync_engine import sync_user

    gateway.server.mit_uebergaengen(uebergaenge_an)
    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))

    ergebnis = sync_user(session, "Alex", gateway=gateway)

    assert ergebnis.ok and ergebnis.item_count == 5
    assert [x.title for x in gateway.server.playlists()[0].items()][0] == "Showdown"


def test_a_missing_library_does_not_break_the_sync(session, gateway, uebergaenge_an):
    """Ist die Bibliothek in Plex nicht angelegt, läuft der Sync trotzdem."""
    from app.sync_engine import sync_user

    db.add_transition_clip(session, "Alex", date(1985, 2, 22),
                           (date(1985, 1, 1), date(1985, 12, 31)), "x.mp4", "X", 1)
    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))

    ergebnis = sync_user(session, "Alex", gateway=gateway)   # server ohne Bibliothek

    assert ergebnis.ok and ergebnis.item_count == 5


# ---------------------------------------------------------------------------
# Nur ein Profil bekommt Clips
# ---------------------------------------------------------------------------


def test_only_the_configured_profile_gets_transitions(uebergaenge_an):
    from app.config import get_settings

    einstellungen = get_settings()
    assert einstellungen.transitions_for("Alex")
    assert not einstellungen.transitions_for("Leo")


def test_an_empty_profile_setting_means_everyone(uebergaenge_an, monkeypatch):
    from app import config

    monkeypatch.setenv("PTM_TRANSITION_USER", "")
    config.get_settings.cache_clear()

    assert config.get_settings().transitions_for("Leo")


def test_disabled_transitions_win_over_the_profile(uebergaenge_an, monkeypatch):
    from app import config

    monkeypatch.setenv("PTM_TRANSITIONS_ENABLED", "false")
    config.get_settings.cache_clear()

    assert not config.get_settings().transitions_for("Alex")


def test_other_profiles_do_not_trigger_a_build(session, gateway, uebergaenge_an):
    """Ein Sync für ein anderes Profil rendert nichts – das spart Minuten."""
    from app import sync_engine

    db.set_period(session, "Leo", date(1985, 1, 1), date(1985, 12, 31))
    angefragt: list[str] = []
    sync_engine.sync_user(session, "Leo", gateway=gateway)

    class Merker:
        def request_transition_build(self, user_id):
            angefragt.append(user_id)

    # Auch mit laufendem Scheduler bleibt es bei nichts.
    sync_engine._request_transition_build(
        session, "Leo", _items(gateway), (date(1985, 1, 1), date(1985, 12, 31))
    )
    assert angefragt == []
    assert list(uebergaenge_an.glob("*.mp4")) == []


# ---------------------------------------------------------------------------
# Zwei Phasen: erst rendern, später einlesen und die Playlist neu bauen
# ---------------------------------------------------------------------------


def _with_scheduler(body):
    """Scheduler in einem eigenen Event-Loop starten und wieder stoppen."""
    import asyncio

    from app.scheduler import SyncScheduler
    from app.scheduler import set_scheduler

    async def runner():
        scheduler = SyncScheduler()
        scheduler.start()
        set_scheduler(scheduler)
        try:
            return body(scheduler)
        finally:
            set_scheduler(None)
            scheduler.shutdown()

    return asyncio.run(runner())


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_rendering_only_queues_the_scan_and_does_not_touch_the_playlist(
    session, gateway, uebergaenge_an, monkeypatch
):
    """Phase 1 rendert – die Playlist bleibt bis zum späteren Einlesen unberührt."""
    from app.plex_client import set_gateway
    from app.scheduler import PUBLISH_JOB_PREFIX, run_transition_build

    monkeypatch.setenv("PTM_TRANSITION_SCAN_DELAY_SECONDS", "300")
    from app import config

    config.get_settings.cache_clear()

    gateway.server.mit_uebergaengen(uebergaenge_an)
    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    set_gateway(gateway)

    def check(scheduler):
        run_transition_build("Alex")
        job = scheduler.scheduler.get_job(f"{PUBLISH_JOB_PREFIX}Alex")
        assert job is not None
        wartezeit = (job.next_run_time - datetime.now(timezone.utc)).total_seconds()
        assert 240 < wartezeit <= 300      # rund fünf Minuten später
        return job

    try:
        _with_scheduler(check)
    finally:
        set_gateway(None)

    assert len(list(uebergaenge_an.glob("*.mp4"))) == 2   # gerendert ist gerendert
    assert gateway.server.playlists() == []               # aber noch nichts gebaut


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_the_scan_phase_rebuilds_the_playlist_with_the_clips(
    session, gateway, uebergaenge_an
):
    """Phase 2 liest Plex ein und baut die Playlist samt Übergängen neu."""
    from app.plex_client import set_gateway
    from app.scheduler import run_transition_publish

    gateway.server.mit_uebergaengen(uebergaenge_an)
    periode = (date(1985, 1, 1), date(1985, 12, 31))
    db.set_period(session, "Alex", *periode)
    transition_build.build_clips(session, "Alex", _items(gateway), periode, gateway)
    vorher = gateway.server.transition_section.scans

    set_gateway(gateway)
    try:
        run_transition_publish("Alex")
    finally:
        set_gateway(None)

    assert gateway.server.transition_section.scans > vorher       # Bibliothek eingelesen
    namen = [x.title for x in gateway.server.playlists()[0].items()]
    assert namen[0].startswith("Time Machine - Friday 22.02.1985")
    assert namen[3].startswith("Time Machine - Wednesday 03.07.1985")


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_invisible_clips_are_retried_before_the_playlist_is_built(
    session, gateway, uebergaenge_an, monkeypatch
):
    """Sieht Plex die Clips noch nicht, wird später erneut nachgesehen."""
    from app.plex_client import set_gateway
    from app.scheduler import PUBLISH_JOB_PREFIX, run_transition_publish

    monkeypatch.setattr(transition_build, "SCAN_TIMEOUT_SECONDS", 0)
    gateway.server.mit_uebergaengen(uebergaenge_an)
    periode = (date(1985, 1, 1), date(1985, 12, 31))
    db.set_period(session, "Alex", *periode)
    transition_build.build_clips(session, "Alex", _items(gateway), periode, gateway)

    # Plex bleibt blind: der Scan macht nichts sichtbar.
    monkeypatch.setattr(gateway.server.transition_section, "update", lambda **k: None)
    set_gateway(gateway)

    def check(scheduler):
        run_transition_publish("Alex", attempt=1)
        assert scheduler.scheduler.get_job(f"{PUBLISH_JOB_PREFIX}Alex") is not None
        assert gateway.server.playlists() == []      # noch nichts gebaut

        # Letzter Versuch: die Playlist entsteht auch ohne die fehlenden Clips.
        run_transition_publish("Alex", attempt=3)

    try:
        _with_scheduler(check)
    finally:
        set_gateway(None)

    namen = [x.title for x in gateway.server.playlists()[0].items()]
    assert namen == ["Showdown", "Brazil", "Zurück in die Zukunft", "Pilot", "Folge 2"]
