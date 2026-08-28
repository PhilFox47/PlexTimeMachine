"""Sendeplätze: wann am Tag ein Titel läuft – wie im Fernsehprogramm.

Innerhalb eines Tages entscheidet nicht mehr das Alphabet über die Reihenfolge,
sondern die Uhrzeit. Serien bekommen ihren Platz in der Vorschau zugewiesen und
behalten ihn; Filme laufen immer zur Primetime.

Das Modul ist bewusst frei von Datenbank und Plex: es bekommt die gespeicherten
Plätze als einfaches ``{ratingKey: "HH:MM"}`` gereicht.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any, Iterable, Optional, Sequence

#: Serien ohne eigenen Platz laufen vormittags.
DEFAULT_SERIES_SLOT = "10:00"

#: Filme immer zur Primetime – nicht einstellbar, das ist der Hauptfilm.
MOVIE_SLOT = "20:15"

#: "20:15", "20.15" und "2015" sind alle gemeint.
_MIT_TRENNER = re.compile(r"^(\d{1,2})[:.](\d{1,2})$")
_OHNE_TRENNER = re.compile(r"^(\d{1,2})(\d{2})$")


def normalise(value: Optional[str]) -> Optional[str]:
    """"9:5" oder "2015" zu "09:05" bzw. "20:15" – None, wenn es keine Zeit ist."""
    if value is None:
        return None
    text = str(value).strip()
    treffer = _MIT_TRENNER.match(text) or _OHNE_TRENNER.match(text)
    if not treffer:
        return None
    stunde, minute = int(treffer.group(1)), int(treffer.group(2))
    if stunde > 23 or minute > 59:
        return None
    return f"{stunde:02d}:{minute:02d}"


def to_minutes(slot: str) -> int:
    """Minuten seit Mitternacht – der eigentliche Sortierwert."""
    sauber = normalise(slot) or DEFAULT_SERIES_SLOT
    stunde, minute = sauber.split(":")
    return int(stunde) * 60 + int(minute)


def group_key(item: Any) -> str:
    """Was zusammen läuft, bleibt zusammen: Serie bzw. einzelner Film."""
    if getattr(item, "is_episode", False):
        return str(item.blacklist_key or item.rating_key)
    return str(item.rating_key)


def slot_for(item: Any, slots: dict[str, str]) -> str:
    """Sendeplatz eines Treffers – Film, gespeicherte Serie oder Standard."""
    if not getattr(item, "is_episode", False):
        return MOVIE_SLOT
    gespeichert = slots.get(group_key(item))
    return normalise(gespeichert) or DEFAULT_SERIES_SLOT


def shuffle_key(day: Optional[date], schluessel: str) -> str:
    """Feste Zufallsreihenfolge für gleiche Uhrzeiten.

    Bewusst gehasht statt gewürfelt: die Reihenfolge sieht willkürlich aus,
    bleibt für denselben Tag aber über alle Läufe hinweg gleich. Sonst würde
    die Playlist bei jedem Sync neu durchgemischt.
    """
    roh = f"{day.isoformat() if day else '-'}|{schluessel}"
    return hashlib.md5(roh.encode("utf-8")).hexdigest()


def sort_key(item: Any) -> tuple:
    """Tag, Uhrzeit, Zufall – und innerhalb einer Serie die Folgennummer."""
    schluessel = group_key(item)
    return (
        item.air_date or date.max,
        to_minutes(getattr(item, "slot", "") or DEFAULT_SERIES_SLOT),
        shuffle_key(item.air_date, schluessel),
        schluessel,
        item.season if item.season is not None else -1,
        item.episode if item.episode is not None else -1,
        (item.title or "").lower(),
    )


def apply_slots(items: Iterable[Any], slots: dict[str, str]) -> list:
    """Jedem Treffer seinen Sendeplatz geben und danach sortieren."""
    liste = list(items)
    for item in liste:
        item.slot = slot_for(item, slots)
    liste.sort(key=sort_key)
    return liste


def slots_in_use(items: Sequence[Any]) -> dict[str, str]:
    """Belegte Plätze je Serie – für die Anzeige in der Vorschau."""
    return {
        group_key(item): item.slot
        for item in items
        if getattr(item, "is_episode", False) and getattr(item, "slot", "")
    }
