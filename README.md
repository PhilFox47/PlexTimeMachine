# Plex Time Machine

Ein selbst gehosteter Zeitreise-Dienst für Plex: Zeitraum wählen, Vorschau aller
**noch nicht gesehenen** Filme und Episoden aus dieser Ära prüfen, Unerwünschtes
dauerhaft auf die Blacklist setzen – und per Knopfdruck **eine feste, dauerhaft
gepflegte Playlist** aktualisieren. Keine neue Playlist pro Suche.

```
   01.01.1982  ▶▶  31.12.1986
   ────────────────────────────
   7 Treffer · 5 Filme · 2 Episoden · 2 geblockt
```

## Was es macht

- **Ein aktueller Zeitraum pro Reisendem** – jederzeit über die UI änderbar,
  keine Profil-Verwaltung.
- **Blacklist pro Nutzer** – wirkt dauerhaft auf alle künftigen Zeitreisen.
  Bei Serien wird die ganze Serie ausgeschlossen (über `grandparentRatingKey`).
- **Eine feste Playlist pro Plex-Nutzer** – wird bei jeder Zeitreise geleert und
  chronologisch neu befüllt.
- **Watched-Status je Betrachter** – jede Zeitreise läuft im Kontext des
  jeweiligen Home-Users (siehe [Multi-User](#multi-user-und-watched-status)).
- **Automatisches Nachziehen** – periodisches Polling plus optionaler
  Plex-Webhook, damit gesehene Titel zeitnah aus der Playlist fallen.
- **Logbuch** – jede ausgeführte Zeitreise mit Zeitraum, Auslöser und Trefferzahl.

## Multi-User und Watched-Status

Plex-Playlists haben eine feste Item-Liste. Ob ein Item *enthalten* ist, wird
beim Bauen anhand des Unwatched-Status **eines** Accounts entschieden – das lässt
sich nicht dynamisch pro Betrachter umbiegen. Deshalb bekommt jede Person genau
eine dauerhafte Playlist:

```
Plex Time Machine – Alex
Plex Time Machine – Nina
```

Der Admin-Token holt sich für jeden Home-User über plex.tv einen
server-spezifischen Token (`MyPlexUser.get_token`). Suche *und* Playlist laufen
damit unter dem jeweiligen Account – jeder sieht nur, was er selbst noch nicht
geschaut hat.

**Nicht abgedeckt (v1):** separat eingeladene „Friends“-Accounts mit eigenem
Plex-Login. Dafür bräuchte jede Person einen eigenen OAuth-PIN-Login-Flow mit
separat gespeichertem Token – eigene Ausbaustufe.

## Schnellstart mit Docker

```bash
git clone https://github.com/PhilFox47/PlexTimeMachine.git
cd PlexTimeMachine
cp .env.example .env
$EDITOR .env            # mindestens PTM_PLEX_BASEURL, PTM_PLEX_TOKEN, Bibliotheksnamen
docker compose up -d
```

Danach läuft die UI auf <http://localhost:8088>. Die SQLite-Datei liegt im
gemounteten Volume `./data`.

## Lokal ohne Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
uvicorn app.main:app --reload --port 8080
```

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen mit dem Präfix `PTM_`
(oder aus einer `.env` im Arbeitsverzeichnis).

| Variable | Default | Bedeutung |
|---|---|---|
| `PTM_PLEX_BASEURL` | `http://localhost:32400` | Basis-URL des Plex Media Servers |
| `PTM_PLEX_TOKEN` | – | Admin-Token des Servers ([Anleitung](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| `PTM_MOVIE_LIBRARY` | `Filme` | Name der Film-Bibliothek |
| `PTM_TV_LIBRARY` | `Serien` | Name der Serien-Bibliothek |
| `PTM_PLAYLIST_NAME_TEMPLATE` | `Plex Time Machine – {user}` | `{user}` wird durch den Home-User ersetzt |
| `PTM_POLL_INTERVAL_MINUTES` | `30` | Periodisches Nachziehen; `0` schaltet es ab |
| `PTM_WEBHOOK_DEBOUNCE_SECONDS` | `20` | Sammelfenster für Webhook-Events |
| `PTM_WEBHOOK_TOKEN` | – | Optionales Geheimnis für `/webhook/plex?token=…` |
| `PTM_DATABASE_URL` | `sqlite:///./data/plex_time_machine.db` | Speicherort der SQLite-DB |
| `PTM_PREVIEW_LIMIT` | `400` | Maximale Zeilen in der Vorschau (`0` = unbegrenzt) |

Die Playlist enthält immer **alle** Treffer – das Limit betrifft nur die Anzeige.

## Plex-Webhook einrichten (optional, Plex Pass)

1. In Plex: **Einstellungen → Webhooks → Webhook hinzufügen**
2. URL eintragen: `http://<host>:8088/webhook/plex`
   (mit gesetztem `PTM_WEBHOOK_TOKEN`: `…/webhook/plex?token=<geheim>`)

Verarbeitet werden `media.scrobble`, `media.rate` und `library.new`; mehrere
Events kurz hintereinander lösen nur einen Sync aus. Manuell gesetzte
„gesehen“-Markierungen erzeugen laut Plex **kein** Webhook-Event – dafür ist das
periodische Polling da.

## Bedienung

1. **Cockpit** – Zeitraum über die Datumsfelder oder die Jahrzehnt-Presets
   wählen. Die Vorschau lädt live, ohne etwas in Plex zu verändern.
2. **Blacklist** – das ⊘-Symbol in einer Zeile schließt Film bzw. Serie dauerhaft
   aus; die Vorschau aktualisiert sich sofort. Verwalten und freigeben unter
   *Blacklist*.
3. **Zeitreise starten** – erst dieser Knopf schreibt die Auswahl in die feste
   Playlist des aktuellen Reisenden.
4. **Reisender** – der Umschalter oben wechselt den Home-User-Kontext
   (Vorschau, Blacklist, Playlist und Logbuch sind pro Nutzer getrennt).

## Architektur

```
app/
├── main.py          FastAPI: Seiten, htmx-Fragmente, Webhook, Thumb-Proxy
├── config.py        Settings aus Env-Variablen
├── db.py            SQLModel/SQLite: UserState, BlacklistEntry, JourneyLog
├── plex_client.py   plexapi-Wrapper inkl. Home-User-Impersonation
├── sync_engine.py   Suche, Blacklist-Filter, Merge/Sort, Playlist-Pflege
├── scheduler.py     APScheduler: Polling + entprellte Webhook-Syncs
├── templates/       Jinja2 (dashboard, blacklist, logbook, partials)
└── static/          Theme-CSS und lokal abgelegtes htmx
```

**Sync-Ablauf** (`sync_engine.sync_user`):

1. Verbindung im Kontext des Home-Users aufbauen
2. Filme und Episoden im Zeitraum suchen (`originallyAvailableAt`, `unwatched`)
3. Ergebnisse exakt auf den Zeitraum eingrenzen (Plex' Datumsfilter sind
   randscharf-exklusiv, deshalb serverseitig weiten und clientseitig prüfen)
4. Blacklist anwenden: Filme über `ratingKey`, Episoden über `grandparentRatingKey`
5. Chronologisch sortieren (Datum → Film vor Episode → Serie → Staffel/Folge)
6. Feste Playlist leeren und in dieser Reihenfolge neu befüllen
   (ohne Treffer wird sie entfernt – Plex kann keine leere Playlist halten)
7. `UserState` fortschreiben und Logbuch-Eintrag anlegen

Ausgelöst wird das durch den UI-Knopf, eine Blacklist-Änderung, das Polling oder
ein Webhook-Event.

## Endpunkte

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/` | Cockpit mit Zeit-Display und Vorschau |
| `GET` | `/blacklist`, `/logbook` | Blacklist-Verwaltung, Reise-Logbuch |
| `POST` | `/period` | Zeitraum speichern, Vorschau-Fragment zurückgeben |
| `GET` | `/preview` | Vorschau-Fragment ohne Speichern |
| `POST` | `/blacklist/add`, `/blacklist/remove` | Blacklist pflegen |
| `POST` | `/sync` | Zeitreise ausführen (Playlist schreiben) |
| `POST` | `/user/select` | Home-User-Kontext wechseln (Cookie) |
| `POST` | `/webhook/plex` | Plex-Webhook-Empfänger |
| `GET` | `/thumb?path=…` | Poster-Proxy (kein Plex-Token im Browser) |
| `GET` | `/healthz` | Status für Monitoring/Healthcheck |

## Tests

```bash
pip install pytest httpx
pytest -q
```

Die Suite deckt Suche, Sortierung, Blacklist-Logik, Playlist-Pflege (inkl.
Leeren, Nachfüllen in Blöcken und dem Fall, dass Plex eine leer geräumte
Playlist selbst entfernt), Scheduler-Entprellung und alle HTTP-Endpunkte gegen
ein Plex-Double ab – ein echter Plex-Server wird dafür nicht gebraucht.

## Sicherheitshinweis

Die App kennt keine eigene Anmeldung und geht von einem vertrauenswürdigen
Heimnetz aus. Wer sie ins Internet stellt, sollte einen Reverse-Proxy mit
Authentifizierung davorsetzen und `PTM_WEBHOOK_TOKEN` setzen.

## Offene Punkte

- Friends-Accounts (eigener Plex-Login) statt nur Home-User
- Blacklist auf Episoden-Ebene statt nur ganze Serien/Filme
- Historie zuletzt genutzter Zeiträume als Schnellauswahl
