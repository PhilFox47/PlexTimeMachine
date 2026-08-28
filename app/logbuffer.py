"""Die letzten Logzeilen im Speicher halten – für die Anzeige in der Oberfläche.

Wer die Software im Container betreibt, kommt an ``docker compose logs`` oft
nur umständlich heran. Deshalb hängt sich die App einen kleinen Ringpuffer an
das Logging: die jüngsten Zeilen stehen damit auch im Browser.

Bewusst nur im Arbeitsspeicher: ein Neustart leert den Puffer, dafür wächst
nichts unbemerkt auf der Platte.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

#: So viele Zeilen werden aufgehoben.
CAPACITY = 400


@dataclass(frozen=True)
class LogLine:
    at: datetime
    level: str
    logger: str
    message: str

    @property
    def is_problem(self) -> bool:
        return self.level in {"WARNING", "ERROR", "CRITICAL"}

    @property
    def short_logger(self) -> str:
        """``app.transition_build`` wird zu ``transition_build``.

        Fremde Logger behalten ihren vollen Namen – bei ihnen sagt das letzte
        Stück oft nichts (``apscheduler.executors.default``).
        """
        if self.logger.startswith("app."):
            return self.logger[len("app."):]
        return self.logger


class RingHandler(logging.Handler):
    """Handler, der die letzten Zeilen aufbewahrt statt sie wegzuschreiben."""

    def __init__(self, capacity: int = CAPACITY):
        super().__init__()
        self.lines: deque[LogLine] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text = f"{text} – {logging.Formatter().formatException(record.exc_info)}"
        except Exception:  # pragma: no cover - defekte Formatierung
            text = str(record.msg)
        self.lines.append(
            LogLine(
                at=datetime.fromtimestamp(record.created, tz=timezone.utc),
                level=record.levelname,
                logger=record.name,
                message=text[:1000],
            )
        )


_handler: Optional[RingHandler] = None


def install(level: int = logging.INFO) -> RingHandler:
    """Einmalig an den Wurzel-Logger hängen.

    Der Wurzel-Logger wird dabei mindestens auf ``level`` gestellt – sonst
    kämen INFO-Zeilen gar nicht erst bei einem Handler an und der Puffer bliebe
    bis zur ersten Warnung leer.
    """
    global _handler
    wurzel = logging.getLogger()
    if wurzel.level == logging.NOTSET or wurzel.level > level:
        wurzel.setLevel(level)
    if _handler is None:
        _handler = RingHandler()
        _handler.setLevel(level)
        wurzel.addHandler(_handler)
    return _handler


def lines(
    limit: int = 100,
    only: Optional[Iterable[str]] = None,
    problems_only: bool = False,
) -> list[LogLine]:
    """Die jüngsten Zeilen, neueste zuletzt.

    ``only`` filtert auf Logger-Namen (Teilstrings), ``problems_only`` auf
    Warnungen und Fehler.
    """
    if _handler is None:
        return []
    treffer = list(_handler.lines)
    if only is not None:
        muster = tuple(only)
        treffer = [z for z in treffer if any(m in z.logger for m in muster)]
    if problems_only:
        treffer = [z for z in treffer if z.is_problem]
    return treffer[-limit:]


def clear() -> None:
    if _handler is not None:
        _handler.lines.clear()
