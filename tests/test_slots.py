"""Tests der Sendeplätze: Reihenfolge innerhalb eines Tages, Speicherung, UI."""

from __future__ import annotations

from datetime import date

import pytest

from app import db, slots
from app.slots import DEFAULT_SERIES_SLOT, MOVIE_SLOT
from app.sync_engine import collect_items

ERA_START = date(1985, 1, 1)
ERA_END = date(1985, 12, 31)


# ---------------------------------------------------------------------------
# Zeiten lesen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "eingabe, erwartet",
    [
        ("20:15", "20:15"), ("9:5", "09:05"), ("20.15", "20:15"), ("2015", "20:15"),
        ("930", "09:30"), (" 7:00 ", "07:00"),
        ("25:00", None), ("12:60", None), ("", None), ("abc", None), (None, None),
    ],
)
def test_times_are_read_forgivingly(eingabe, erwartet):
    assert slots.normalise(eingabe) == erwartet


def test_minutes_are_the_sort_value():
    assert slots.to_minutes("00:00") == 0
    assert slots.to_minutes("20:15") == 1215
    assert slots.to_minutes("Unfug") == slots.to_minutes(DEFAULT_SERIES_SLOT)


# ---------------------------------------------------------------------------
# Reihenfolge innerhalb eines Tages
# ---------------------------------------------------------------------------


def test_series_default_to_ten_and_movies_to_primetime(gateway):
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END)

    serien = {i.slot for i in items if i.is_episode}
    filme = {i.slot for i in items if not i.is_episode}

    assert serien == {DEFAULT_SERIES_SLOT} == {"10:00"}
    assert filme == {MOVIE_SLOT} == {"20:15"}


def test_a_late_slot_moves_the_series_behind_the_movie(gateway):
    """Genau der Zweck der Sendeplätze: die Reihenfolge des Tages bestimmen."""
    frueh = collect_items(gateway, gateway.server, ERA_START, ERA_END)
    assert [i.title for i in frueh[:2]] == ["Showdown", "Brazil"]

    spaet = collect_items(gateway, gateway.server, ERA_START, ERA_END, {"200": "22:30"})

    assert [i.title for i in spaet[:2]] == ["Brazil", "Showdown"]
    assert spaet[1].slot == "22:30"


def test_episodes_of_one_series_keep_their_order(gateway):
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END, {"100": "20:15"})

    knight = [i for i in items if i.series_title == "Knight Rider"]
    assert [i.episode for i in knight] == [1, 2]


def test_the_same_time_is_shuffled_but_stays_stable(gateway):
    """Gleiche Uhrzeit: die Reihenfolge sieht willkürlich aus, springt aber nicht.

    Sonst würde die Playlist bei jedem Sync neu durchgemischt.
    """
    gleich = {"100": "20:15", "200": "20:15"}

    erste = [i.title for i in collect_items(gateway, gateway.server, ERA_START, ERA_END, gleich)]
    zweite = [i.title for i in collect_items(gateway, gateway.server, ERA_START, ERA_END, gleich)]

    assert erste == zweite


def test_the_shuffle_differs_between_days_and_titles():
    """Der Zufall hängt an Tag und Titel – nicht alles landet in einer Reihe."""
    tag = date(1985, 2, 22)
    schluessel = {slots.shuffle_key(tag, str(k)) for k in range(30)}

    assert len(schluessel) == 30                                   # keine Kollisionen
    assert slots.shuffle_key(tag, "7") != slots.shuffle_key(date(1985, 2, 23), "7")


# ---------------------------------------------------------------------------
# Speicherung
# ---------------------------------------------------------------------------


def test_slots_are_stored_and_can_be_cleared(session):
    db.set_slot(session, "200", "22:30", "Das A-Team")

    assert db.all_slots(session) == {"200": "22:30"}
    assert db.get_slot(session, "200").title == "Das A-Team"

    db.set_slot(session, "200", "21:45")
    assert db.all_slots(session)["200"] == "21:45"
    assert db.get_slot(session, "200").title == "Das A-Team"      # Titel bleibt

    assert db.clear_slot(session, "200") is True
    assert db.all_slots(session) == {}
    assert db.clear_slot(session, "200") is False


def test_a_stored_slot_survives_for_the_next_journey(session, gateway):
    """„gilt ab sofort für die Serie" – auch für jeden späteren Lauf."""
    from app.sync_engine import sync_user

    db.set_slot(session, "200", "22:30", "Das A-Team")
    db.set_period(session, "Alex", ERA_START, ERA_END)

    sync_user(session, "Alex", gateway=gateway)

    titel = [x.title for x in gateway.server.playlists()[0].items()]
    assert titel[:2] == ["Brazil", "Showdown"]


# ---------------------------------------------------------------------------
# Bedienung
# ---------------------------------------------------------------------------


def test_the_preview_offers_a_slot_for_series_only(client):
    seite = client.get("/preview?start=1985-01-01&end=1985-12-31").text

    assert 'class="slot-input"' in seite                    # Serien: änderbar
    assert 'value="10:00"' in seite
    assert 'class="slot-fixed"' in seite and ">20:15<" in seite   # Filme: fest


def test_the_slot_is_saved_only_when_the_field_is_left(client):
    """Beim Tippen darf sich die Zeile nicht schon wegbewegen.

    Ein Zeitfeld meldet jede vollständige Eingabe als ``change`` – die Vorschau
    hätte sich also schon zwischen Stunde und Minute neu sortiert.
    """
    seite = client.get("/preview?start=1985-01-01&end=1985-12-31").text

    assert "blur changed" in seite
    assert "keyup[key=='Enter'] changed" in seite      # Enter bestätigt auch
    assert 'hx-trigger="change"' not in seite


def test_setting_a_slot_reorders_the_preview_right_away(client, session):
    antwort = client.post(
        "/slot",
        data={"rating_key": "200", "slot": "22:30", "title": "Das A-Team",
              "start": "1985-01-01", "end": "1985-12-31"},
    )

    assert antwort.status_code == 200
    assert db.all_slots(session) == {"200": "22:30"}
    # Brazil (20:15) steht jetzt vor dem A-Team (22:30)
    seite = antwort.text
    assert seite.index("Brazil") < seite.index("Showdown")


def test_an_empty_slot_falls_back_to_the_default(client, session):
    db.set_slot(session, "200", "22:30", "Das A-Team")

    client.post("/slot", data={"rating_key": "200", "slot": "",
                               "start": "1985-01-01", "end": "1985-12-31"})

    assert db.all_slots(session) == {}


def test_the_clip_shows_the_slot_of_every_title(session, gateway):
    """Die Übergänge zeigen dieselben Zeiten – „up next" wie im Fernsehen."""
    from app import transition_build

    db.set_slot(session, "200", "22:30", "Das A-Team")
    items = collect_items(gateway, gateway.server, ERA_START, ERA_END, db.all_slots(session))
    tag, tages_items = transition_build.group_by_day(items)[0]

    spec = transition_build.spec_for_day(gateway.server, date(1985, 2, 21), tag, tages_items)

    assert [(i.title, i.slot) for i in spec.items] == [("Brazil", "20:15"), ("Showdown", "22:30")]
