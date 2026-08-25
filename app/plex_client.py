"""plexapi-Wrapper inklusive Home-User-Impersonation.

Der Admin-Token ist die einzige Anmeldeinformation, die konfiguriert werden
muss.  Für jeden Home-User wird über die Plex-Cloud ein server-spezifischer
Token geholt (``MyPlexUser.get_token``); damit werden sowohl die Suchen als
auch die Playlist im Kontext des jeweiligen Accounts ausgeführt.  Nur so
stimmt der Unwatched-Status pro Person.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from plexapi.exceptions import NotFound, Unauthorized
from plexapi.server import PlexServer

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

#: Wie lange ein geholter Home-User-Token wiederverwendet wird.
TOKEN_TTL_SECONDS = 30 * 60

#: Die Liste der Home-User steckt hinter einem plex.tv-Aufruf. Ohne diesen
#: kurzen Puffer würde jeder Seitenaufruf ins Internet greifen.
USERS_TTL_SECONDS = 60

#: Ist Plex nicht erreichbar, wird auch das kurz gemerkt – sonst kostet jeder
#: Seitenaufruf erneut das volle Verbindungs-Timeout.
USERS_FAILURE_TTL_SECONDS = 15


class PlexUnavailable(RuntimeError):
    """Der Plex-Server ist nicht erreichbar oder nicht konfiguriert."""


@dataclass(frozen=True)
class HomeUser:
    """Ein Plex-Home-User (inkl. Server-Admin)."""

    id: str
    title: str
    is_admin: bool = False
    thumb: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} (Admin)" if self.is_admin else self.title


class PlexGateway:
    """Zentraler Zugang zu Plex – cached Verbindungen und Tokens."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._lock = threading.RLock()
        self._admin: Optional[PlexServer] = None
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._server_cache: dict[str, tuple[str, PlexServer]] = {}
        self._users_cache: Optional[tuple[list[HomeUser], str, float]] = None

    # -- Verbindungen ------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    def _connect(self, token: str) -> PlexServer:
        """Verbindung aufbauen – bewusst mit Timeout, damit nichts ewig hängt."""
        return PlexServer(
            self._settings.plex_baseurl,
            token,
            timeout=self._settings.plex_timeout_seconds,
        )

    def admin_server(self) -> PlexServer:
        """Verbindung mit dem Admin-Token (cached).

        Der Verbindungsaufbau passiert bewusst *außerhalb* der Sperre: sonst
        wartet jeder andere Aufruf (etwa ein Seitenaufruf) so lange, wie das
        Netzwerk-Timeout dauert.
        """
        if not self._settings.configured:
            raise PlexUnavailable(
                "Plex ist nicht konfiguriert – bitte PTM_PLEX_BASEURL und "
                "PTM_PLEX_TOKEN setzen."
            )
        with self._lock:
            if self._admin is not None:
                return self._admin

        try:
            server = self._connect(self._settings.plex_token)
        except Unauthorized as exc:
            raise PlexUnavailable(f"Plex-Token abgelehnt: {exc}") from exc
        except Exception as exc:  # Netzwerk, DNS, Timeout ...
            raise PlexUnavailable(
                f"Plex nicht erreichbar unter {self._settings.plex_baseurl}: {exc}"
            ) from exc

        with self._lock:
            if self._admin is None:
                self._admin = server
            return self._admin

    def invalidate(self) -> None:
        """Alle Caches verwerfen (z. B. nach Konfigurationswechsel oder Fehler)."""
        with self._lock:
            self._admin = None
            self._token_cache.clear()
            self._server_cache.clear()
            self._users_cache = None

    # -- Home-User ---------------------------------------------------------

    def home_users(self) -> list[HomeUser]:
        """Admin + alle Home-User des Servers – kurz zwischengespeichert."""
        now = time.monotonic()
        with self._lock:
            cached = self._users_cache
        if cached is not None and cached[2] > now:
            if cached[1]:
                raise PlexUnavailable(cached[1])
            return list(cached[0])

        try:
            users = self._load_home_users()
        except PlexUnavailable as exc:
            with self._lock:
                self._users_cache = ([], str(exc), now + USERS_FAILURE_TTL_SECONDS)
            raise
        with self._lock:
            self._users_cache = (users, "", now + USERS_TTL_SECONDS)
        return list(users)

    def _load_home_users(self) -> list[HomeUser]:
        """Admin + alle Home-User des Servers.

        Fällt auf "nur Admin" zurück, wenn plex.tv nicht erreichbar ist –
        die App bleibt damit auch offline benutzbar.
        """
        server = self.admin_server()
        admin_title = "Admin"
        users: list[HomeUser] = []
        try:
            account = server.myPlexAccount()
            admin_title = account.title or account.username or "Admin"
            users.append(
                HomeUser(
                    id=admin_title,
                    title=admin_title,
                    is_admin=True,
                    thumb=getattr(account, "thumb", "") or "",
                )
            )
            for user in account.users():
                if not getattr(user, "home", False):
                    continue  # Friends-Accounts: nicht Teil von v1
                title = user.title or user.username
                if not title:
                    continue
                users.append(
                    HomeUser(id=title, title=title, thumb=getattr(user, "thumb", "") or "")
                )
        except PlexUnavailable:
            raise
        except Exception as exc:
            log.warning("Home-User konnten nicht geladen werden (%s) – nutze Admin", exc)
            if not users:
                users.append(HomeUser(id=admin_title, title=admin_title, is_admin=True))
        return users

    def _user_token(self, user_id: str) -> Optional[str]:
        """Server-spezifischer Token eines Home-Users; None => Admin-Token."""
        now = time.time()
        with self._lock:
            cached = self._token_cache.get(user_id)
            if cached and cached[1] > now:
                return cached[0]

        server = self.admin_server()
        account = server.myPlexAccount()
        if (account.title or account.username) == user_id:
            return None  # Admin selbst

        try:
            token = account.user(user_id).get_token(server.machineIdentifier)
        except NotFound as exc:
            raise PlexUnavailable(f"Unbekannter Home-User: {user_id}") from exc
        except Exception as exc:
            raise PlexUnavailable(
                f"Token für Home-User '{user_id}' konnte nicht geholt werden: {exc}"
            ) from exc

        with self._lock:
            self._token_cache[user_id] = (token, now + TOKEN_TTL_SECONDS)
        return token

    def connect_as(self, user_id: str) -> PlexServer:
        """Server-Verbindung im Kontext eines Home-Users."""
        token = self._user_token(user_id)
        if token is None:
            return self.admin_server()

        with self._lock:
            cached = self._server_cache.get(user_id)
            if cached and cached[0] == token:
                return cached[1]  # Token unverändert -> Verbindung wiederverwenden
        try:
            server = self._connect(token)
        except Exception as exc:
            raise PlexUnavailable(
                f"Verbindung als '{user_id}' fehlgeschlagen: {exc}"
            ) from exc
        with self._lock:
            self._server_cache[user_id] = (token, server)
        return server

    # -- Bibliotheken ------------------------------------------------------

    def movie_section(self, server: PlexServer):
        return self._section(server, self._settings.movie_library, "movie")

    def tv_section(self, server: PlexServer):
        return self._section(server, self._settings.tv_library, "show")

    @staticmethod
    def _section(server: PlexServer, name: str, expected_type: str):
        try:
            section = server.library.section(name)
        except NotFound as exc:
            raise PlexUnavailable(f"Bibliothek '{name}' existiert nicht.") from exc
        except Exception as exc:
            raise PlexUnavailable(f"Bibliothek '{name}' nicht lesbar: {exc}") from exc
        if section.type != expected_type:
            raise PlexUnavailable(
                f"Bibliothek '{name}' ist vom Typ '{section.type}', "
                f"erwartet wurde '{expected_type}'."
            )
        return section


_gateway: Optional[PlexGateway] = None
_gateway_lock = threading.Lock()


def get_gateway() -> PlexGateway:
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = PlexGateway()
        return _gateway


def set_gateway(gateway: Optional[PlexGateway]) -> None:
    """Test-Hook: Gateway ersetzen oder zurücksetzen."""
    global _gateway
    with _gateway_lock:
        _gateway = gateway
