"""Cover-Bilder für die Playlists: prüfen, ablegen, wiederfinden.

Die Dateien liegen neben der Datenbank im Datenverzeichnis. Plex bekommt sie
beim Sync als Poster übertragen (``uploadPoster`` schickt die Bytes, der
Plex-Server muss die Datei also nicht selbst erreichen können).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from app.config import get_settings

log = logging.getLogger(__name__)

#: Erkennungsmerkmale am Dateianfang -> Endung. Nur diese Formate nimmt Plex
#: zuverlässig als Poster an.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class CoverError(ValueError):
    """Das hochgeladene Bild ist unbrauchbar."""


def cover_dir() -> Path:
    path = Path(get_settings().cover_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def detect_extension(data: bytes) -> Optional[str]:
    """Bildformat am Inhalt erkennen – der Dateiname wird nicht geglaubt."""
    for signature, extension in SIGNATURES:
        if data.startswith(signature):
            return extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def content_type_for(filename: str) -> str:
    return CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def almanach_stem(almanach_id: int) -> str:
    return f"almanach-{int(almanach_id)}"


def timemachine_stem(user_id: str) -> str:
    """Nutzernamen können alles enthalten – daher ein stabiler Kurz-Hash."""
    digest = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:12]
    return f"timemachine-{digest}"


def store(stem: str, data: bytes) -> str:
    """Bild prüfen und ablegen; gibt den Dateinamen zurück."""
    settings = get_settings()
    if not data:
        raise CoverError("Die Datei ist leer.")
    if len(data) > settings.cover_max_bytes:
        limit = settings.cover_max_bytes // (1024 * 1024)
        raise CoverError(f"Das Bild ist größer als {limit} MB.")

    extension = detect_extension(data)
    if extension is None:
        raise CoverError("Nur JPEG, PNG, GIF oder WebP werden unterstützt.")

    remove(stem)  # alte Fassung (ggf. mit anderer Endung) verwerfen
    filename = f"{stem}{extension}"
    (cover_dir() / filename).write_bytes(data)
    return filename


def path_for(filename: Optional[str]) -> Optional[Path]:
    """Pfad zu einem abgelegten Cover – None, wenn es (nicht mehr) existiert."""
    if not filename:
        return None
    # Der Dateiname stammt immer aus der eigenen Datenbank; zur Sicherheit wird
    # trotzdem nur der reine Name verwendet, nie ein zusammengesetzter Pfad.
    candidate = cover_dir() / Path(filename).name
    return candidate if candidate.is_file() else None


def copy(source_filename: Optional[str], target_stem: str) -> Optional[str]:
    """Cover einer Sammlung für eine Kopie übernehmen."""
    source = path_for(source_filename)
    if source is None:
        return None
    try:
        return store(target_stem, source.read_bytes())
    except CoverError as exc:  # pragma: no cover - abgelegte Datei war gültig
        log.warning("Cover konnte nicht kopiert werden: %s", exc)
        return None


def remove(stem: str) -> bool:
    """Alle Fassungen eines Covers löschen."""
    removed = False
    for existing in cover_dir().glob(f"{stem}.*"):
        existing.unlink(missing_ok=True)
        removed = True
    return removed
