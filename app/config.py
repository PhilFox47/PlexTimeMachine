"""Konfiguration aus Umgebungsvariablen (Prefix ``PTM_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    playlist_name_template: str = "Plex Time Machine – {user}"
    almanach_playlist_name_template: str = "Plex Almanach – {user} · {name}"

    # --- Automatisierung --------------------------------------------------
    poll_interval_minutes: int = 30
    webhook_debounce_seconds: int = 20
    webhook_token: str = ""

    # --- Persistenz / UI --------------------------------------------------
    database_url: str = "sqlite:///./data/plex_time_machine.db"
    preview_limit: int = 400
    cover_dir: str = "./data/covers"
    cover_max_bytes: int = 5 * 1024 * 1024

    @property
    def configured(self) -> bool:
        """True, sobald ein Token hinterlegt ist – sonst läuft die UI im Demo-Modus."""
        return bool(self.plex_token and self.plex_baseurl)

    def playlist_name_for(self, user: str) -> str:
        return self.playlist_name_template.format(user=user)

    def almanach_playlist_name_for(self, user: str, name: str) -> str:
        """Playlist-Name eines benannten Almanachs.

        Ältere Konfigurationen kennen den Platzhalter ``{name}`` noch nicht –
        dann wird er angehängt, damit mehrere Almanachs nicht auf derselben
        Plex-Playlist landen.
        """
        template = self.almanach_playlist_name_template
        if "{name}" not in template:
            template = f"{template} · {{name}}"
        return template.format(user=user, name=name)


@lru_cache
def get_settings() -> Settings:
    return Settings()
