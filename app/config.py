"""Konfiguration aus Umgebungsvariablen (Prefix ``PTM_``)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.formatting import weekday_long


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PTM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Plex -------------------------------------------------------------
    plex_baseurl: str = "http://localhost:32400"
    plex_token: str = ""
    movie_library: str = "Filme"
    tv_library: str = "Serien"
    plex_timeout_seconds: int = 10
    playlist_name_template: str = "{weekday} - {date} - Time Machine"
    almanach_playlist_name_template: str = "{name} – {user} – Almanach"

    # --- Automatisierung --------------------------------------------------
    poll_interval_minutes: int = 30
    webhook_debounce_seconds: int = 20
    webhook_token: str = ""

    # --- Persistenz / UI --------------------------------------------------
    database_url: str = "sqlite:///./data/plex_time_machine.db"
    preview_limit: int = 400
    cover_dir: str = "./data/covers"

    # --- Übergangsclips ---------------------------------------------------
    transitions_enabled: bool = False
    transition_dir: str = "./data/transitions"
    transition_library: str = "Zeitreise-Übergänge"
    transition_max_clips: int = 7
    transition_height: int = 1080
    #: Klang unter der Datumsrolle: leer = mitgelieferter Chime, "off" = stumm,
    #: sonst ein Pfad auf eine eigene Datei.
    transition_sound: str = ""
    #: Logodateien für die Tafel. Leer = app/assets/logo.png bzw.
    #: app/assets/logo_mark.png, "off" = gezeichnetes Zeichen.
    transition_logo: str = ""
    transition_logo_mark: str = ""
    #: Nur für dieses Profil werden Clips gerendert – leer = für alle. Das
    #: Rendern kostet Minuten, und gebraucht wird es praktisch nur beim
    #: Hauptprofil.
    transition_user: str = "Zeitreisende Ente"
    #: Nach dem Rendern erst so lange warten, bevor Plex eingelesen und die
    #: Playlist neu gebaut wird – Plex braucht für frische Dateien Ruhe.
    transition_scan_delay_seconds: int = 300
    ffmpeg_binary: str = "ffmpeg"
    cover_max_bytes: int = 5 * 1024 * 1024

    def transitions_for(self, user: str) -> bool:
        """Bekommt dieses Profil Übergangsclips?"""
        if not self.transitions_enabled:
            return False
        gewuenscht = self.transition_user.strip()
        return not gewuenscht or gewuenscht.casefold() == (user or "").strip().casefold()

    @property
    def configured(self) -> bool:
        """True, sobald ein Token hinterlegt ist – sonst läuft die UI im Demo-Modus."""
        return bool(self.plex_token and self.plex_baseurl)

    def playlist_name_for(self, user: str, first_date: Optional[date] = None) -> str:
        """Name der Zeitreise-Playlist.

        ``first_date`` ist das Erscheinungsdatum des ältesten Titels *in* der
        Playlist – daraus ergeben sich ``{weekday}`` und ``{date}``. Der Name
        wandert damit mit, sobald der früheste Titel weggesehen ist.
        """
        return self.playlist_name_template.format(
            user=user,
            weekday=weekday_long(first_date),
            date=first_date.strftime("%d.%m.%Y") if first_date else "",
        ).strip()

    def almanach_playlist_name_for(self, user: str, name: str) -> str:
        """Playlist-Name eines benannten Almanachs.

        Ältere Konfigurationen kennen den Platzhalter ``{name}`` noch nicht –
        dann wird er vorangestellt, damit mehrere Almanachs nicht auf derselben
        Plex-Playlist landen.
        """
        template = self.almanach_playlist_name_template
        if "{name}" not in template:
            template = f"{{name}} – {template}"
        return template.format(user=user, name=name)


@lru_cache
def get_settings() -> Settings:
    return Settings()
