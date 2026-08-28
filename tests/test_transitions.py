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


def _tafel(anzahl: int, hoehe: int = 360):
    stage = transitions.Stage(hoehe)
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", f"Film {i}", f"Film {i}", year=1985, slot="20:15")
                     for i in range(anzahl)])
    return stage, transitions.Board(stage, spec)


@pytest.mark.parametrize("anzahl", [1, 2, 3])
def test_a_short_day_stands_still(anzahl):
    """Was auf die Tafel passt, wird nicht bewegt."""
    _, board = _tafel(anzahl)

    assert len(board.zeilen) == anzahl
    assert board.max_scroll == 0
    assert board.positionen[0] >= 0
    assert board.positionen[-1] + board.hoehe <= board.fenster_h + 1


@pytest.mark.parametrize("anzahl", [4, 6, 9, 24])
def test_longer_days_scroll_until_the_last_title_was_shown(anzahl):
    """Alles darüber scrollt – und zwar genau so weit, dass nichts fehlt."""
    _, board = _tafel(anzahl)
    dauer = transitions.board_duration(anzahl)

    assert board.max_scroll > 0
    assert board.scroll_at(0.0, dauer) == 0                        # erst stehen
    # Am Ende steht die letzte Zeile vollständig im Fenster.
    letzte = board.positionen[-1] - board.scroll_at(dauer, dauer) + board.hoehe
    assert letzte == pytest.approx(board.fenster_h, abs=2)
    # Die Zeilenhöhe bleibt dieselbe wie bei einer kurzen Liste – die Karten
    # schrumpfen also nie, egal wie voll der Tag ist.
    assert board.hoehe == _tafel(2)[1].hoehe


def test_the_scroll_only_starts_after_the_first_titles_were_read():
    _, board = _tafel(10)
    dauer = transitions.board_duration(10)

    assert board.scroll_at(2.0, dauer) == 0
    mitte = board.scroll_at(dauer / 2, dauer)
    assert 0 < mitte < board.max_scroll
    assert board.scroll_at(dauer - 0.9, dauer) == pytest.approx(board.max_scroll, abs=2)


def test_very_long_days_are_summarised():
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", f"Film {i}", f"Film {i}", year=1985) for i in range(30)])

    assert len(spec.shown) == transitions.MAX_ROWS == 24
    assert spec.extra == 6


def test_duration_follows_the_number_of_titles():
    kurz = transitions.clip_duration(1)
    voll = transitions.clip_duration(3)

    assert kurz < voll
    # Passt der Tag auf eine Tafel, bleibt der Clip etwa so lang wie der Klang.
    assert voll == pytest.approx(8.35, abs=0.4)
    # Danach wächst er gleichmäßig mit jeder weiteren Zeile.
    schritte = [transitions.clip_duration(n) for n in (5, 6, 7)]
    assert all(b - a == pytest.approx(0.85, abs=0.01) for a, b in zip(schritte, schritte[1:]))


def test_a_long_day_ends_with_a_line_for_the_rest():
    """Über der Obergrenze steht keine Karte mehr, sondern eine ruhige Zeile."""
    stage = transitions.Stage(360)
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", f"Film {i}", f"Film {i}", year=1985, slot="20:15")
                     for i in range(transitions.MAX_ROWS + 6)])

    board = transitions.Board(stage, spec)

    assert spec.extra == 6
    assert len(board.zeilen) == transitions.MAX_ROWS + 1          # plus Schlusszeile
    assert board.zeilen[-1].height < board.hoehe                  # flacher als eine Karte


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
    assert "Audio: aac" in beschreibung         # ohne Tonspur stolpern manche Clients


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------


def _logo_datei(pfad, groesse=(1200, 600), farbe=(255, 0, 200, 255)):
    """Eine Logodatei wie die echten: Marke links, viel Luft drumherum."""
    from PIL import Image, ImageDraw

    bild = Image.new("RGBA", groesse, (0, 0, 0, 0))
    ImageDraw.Draw(bild).rectangle([100, 150, 400, 350], fill=farbe)
    bild.save(pfad)
    return pfad


def test_a_logo_file_is_trimmed_and_used(tmp_path):
    """Die echte Datei hat viel leeren Rand – ungeschnitten bliebe ein Krümel."""
    datei = _logo_datei(tmp_path / "logo.png")

    stage = transitions.Stage(360, logo=str(datei), logo_mark="off")

    assert stage.logo.size == (301, 201)         # auf die Marke beschnitten
    bild = stage.base()
    ecke = bild.crop((0, 0, int(0.3 * bild.width), int(0.2 * bild.height)))
    farben = {f for _, f in ecke.getcolors(60000)}
    assert (255, 0, 200) in farben               # das Logo steht oben links


def test_a_missing_logo_falls_back_to_the_drawn_mark(tmp_path):
    stage = transitions.Stage(360, logo=str(tmp_path / "gibtsnicht.png"),
                              logo_mark=str(tmp_path / "auch.png"))

    assert stage.logo is None and stage.logo_mark is None
    stage.lockup(stage.base())                   # zeichnet, statt zu scheitern


def test_off_forces_the_drawn_mark(tmp_path):
    _logo_datei(tmp_path / "logo.png")

    stage = transitions.Stage(360, logo="off")

    assert stage.logo is None


def test_a_logo_without_transparency_is_trimmed_by_its_white_border(tmp_path):
    """Auch eine deckend weiße Datei soll nicht als weißer Kasten landen."""
    from PIL import Image, ImageDraw

    bild = Image.new("RGBA", (800, 400), (255, 255, 255, 255))
    ImageDraw.Draw(bild).rectangle([200, 100, 500, 300], fill=(230, 130, 30, 255))
    bild.save(tmp_path / "weiss.png")

    geladen = transitions.load_logo(str(tmp_path / "weiss.png"), transitions.DEFAULT_LOGO)

    assert geladen.size == (301, 201)


def test_the_chime_ships_with_the_app():
    """Ohne die Datei bliebe jeder Übergang stumm – sie gehört ins Image."""
    assert transitions.DEFAULT_SOUND.exists()
    assert transitions.sound_file(None) == transitions.DEFAULT_SOUND
    assert transitions.sound_file("") == transitions.DEFAULT_SOUND
    assert transitions.sound_file("off") is None
    assert transitions.sound_file("/gibt/es/nicht.aac") is None    # still statt Absturz


def _dauer(pfad) -> float:
    """Länge einer Videodatei in Sekunden, laut FFmpeg."""
    text = subprocess.run(
        [ffmpeg_pfad(), "-hide_banner", "-i", str(pfad)], capture_output=True, text=True
    ).stderr
    roh = text.split("Duration:")[1].split(",")[0].strip()
    stunden, minuten, sekunden = roh.split(":")
    return int(stunden) * 3600 + int(minuten) * 60 + float(sekunden)


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
@pytest.mark.parametrize("anzahl", [1, 9])
def test_the_clip_is_exactly_as_long_as_its_pictures(tmp_path, anzahl):
    """Der Klang bestimmt die Länge nicht – weder der kurze noch der lange Fall.

    Bei einem Titel ist der Clip kürzer als der Chime, bei neun länger; ohne
    festen Schnitt lief die aufgefüllte Stille danach minutenlang weiter.
    """
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("episode", "Serie", f"Folge {i}", 1, i, 2000, slot="20:15")
                     for i in range(anzahl)])
    ziel = tmp_path / f"ton{anzahl}.mp4"

    transitions.render_clip(spec, ziel, height=360, ffmpeg=ffmpeg_pfad())

    assert _dauer(ziel) == pytest.approx(transitions.clip_duration(anzahl), abs=0.15)


@pytest.mark.skipif(ffmpeg_pfad() is None, reason="kein FFmpeg zum Testen vorhanden")
def test_the_rollover_is_backed_by_the_chime(tmp_path):
    """Der mitgelieferte Klang landet als Tonspur im Clip."""
    spec = ClipSpec("MONDAY", "24.08.2026", "TUESDAY", "25.08.2026",
                    [ClipItem("movie", "Film", "Film", year=1985, slot="20:15")])
    mit_ton, ohne_ton = tmp_path / "mit.mp4", tmp_path / "ohne.mp4"

    transitions.render_clip(spec, mit_ton, height=360, ffmpeg=ffmpeg_pfad())
    transitions.render_clip(spec, ohne_ton, height=360, ffmpeg=ffmpeg_pfad(), sound="off")

    beschreibung = subprocess.run(
        [ffmpeg_pfad(), "-hide_banner", "-i", str(mit_ton)], capture_output=True, text=True
    ).stderr
    assert "Audio: aac" in beschreibung
    # Stille lässt sich viel besser packen als der Chime.
    assert mit_ton.stat().st_size > ohne_ton.stat().st_size


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


def test_a_failed_render_is_reported_and_not_swallowed(session, gateway, uebergaenge_an,
                                                       monkeypatch):
    """Scheitert das Rendern, muss der Grund stehen bleiben.

    Vorher überschrieb das anschließende „nichts zu erzeugen" die Fehlermeldung
    – in der Oberfläche stand dann fälschlich alles sei in Ordnung.
    """
    from app.plex_client import set_gateway
    from app.scheduler import run_transition_build

    def kaputt(*a, **kw):
        raise transitions.RenderError("FFmpeg nicht gefunden ('ffmpeg')")

    monkeypatch.setattr(transitions, "render_clip", kaputt)
    db.set_period(session, "Alex", date(1985, 1, 1), date(1985, 12, 31))
    set_gateway(gateway)
    try:
        run_transition_build("Alex")
    finally:
        set_gateway(None)

    stand = db.get_or_create_user_state(session, "Alex")
    assert stand.transition_phase == "error"
    assert "FFmpeg" in stand.transition_message


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
