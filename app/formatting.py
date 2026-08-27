"""Deutsche Datumsformate und Wochenrechnung.

Ein Ort für alle Datumsausgaben, damit Wochentag-Kürzel in Tabellen,
Zeit-Displays und Logbuch identisch aussehen.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Union

#: Zweibuchstabige Wochentagskürzel, indiziert über ``date.weekday()`` (Mo = 0).
WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

#: Ausgeschriebene Wochentage für den Playlist-Namen. Bewusst englisch: so
#: sieht die Playlist in Plex aus, wie sie gewünscht wurde ("Monday - …").
WEEKDAYS_LONG = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

#: Ein Wochenschritt: Start und Ende wandern um genau sieben Tage weiter,
#: der Wochentag bleibt damit erhalten.
WEEK = timedelta(days=7)


def weekday_short(value: Union[date, datetime, None]) -> str:
    """``Mo``, ``Di`` … für ein Datum; leerer String, wenn nichts gesetzt ist."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return WEEKDAYS_SHORT[value.weekday()]


def weekday_long(value: Union[date, datetime, None]) -> str:
    """``Monday``, ``Tuesday`` … – für den Namen der Zeitreise-Playlist."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return WEEKDAYS_LONG[value.weekday()]


def format_date(value: Union[date, datetime, None], fallback: str = "—") -> str:
    """``Mo 31.01.2000`` – Wochentag immer direkt neben dem Datum."""
    if value is None:
        return fallback
    if isinstance(value, datetime):
        value = value.date()
    return f"{weekday_short(value)} {value.strftime('%d.%m.%Y')}"


def format_datetime(value: Optional[datetime], fallback: str = "—") -> str:
    """``Mo 31.01.2000 19:45``."""
    if value is None:
        return fallback
    return f"{format_date(value)} {value.strftime('%H:%M')}"


def format_period(start: Optional[date], end: Optional[date], fallback: str = "—") -> str:
    """``Mo 31.01.2000 – Mo 07.02.2000``."""
    if start is None or end is None:
        return fallback
    return f"{format_date(start)} – {format_date(end)}"


def shift_period(start: date, end: date, weeks: int = 1) -> tuple[date, date]:
    """Zeitraum um ganze Wochen verschieben – Länge und Wochentage bleiben gleich.

    Aus ``31.01.2000 – 07.02.2000`` wird mit ``weeks=1`` also
    ``07.02.2000 – 14.02.2000``: derselbe Zuschnitt, eine Woche später.
    """
    delta = WEEK * weeks
    return start + delta, end + delta


def week_of(anchor: date) -> tuple[date, date]:
    """Montag-bis-Montag-Woche rund um ``anchor``.

    Entspricht dem üblichen Zuschnitt „von diesem Montag bis nächsten Montag“
    (beide Ränder inklusive), wie er beim wochenweisen Durchgehen entsteht.
    """
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + WEEK
