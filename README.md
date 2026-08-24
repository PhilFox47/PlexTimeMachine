# Plex Time Machine

Ein selbst gehosteter Zeitreise-Dienst für Plex: Zeitraum wählen, Vorschau aller
**noch nicht gesehenen** Filme und Episoden aus dieser Ära prüfen, Unerwünschtes
dauerhaft auf die Blacklist setzen – und per Knopfdruck **eine feste, dauerhaft
gepflegte Playlist** aktualisieren. Keine neue Playlist pro Suche.

```
   MO 31.01.2000  ▶▶  MO 07.02.2000
   ─────────────────────────────────
   3 Treffer · 2 Filme · 1 Episode · 0 geblockt
   ◀ Woche zurück   Woche ausrichten   Nächste Woche ▶
```

## Was es macht

- **Ein aktueller Zeitraum pro Reisendem** – jederzeit über die UI änderbar,
  keine Profil-Verwaltung. Der zuletzt gewählte Zeitraum wird gemerkt und beim
  nächsten Aufruf wieder geladen.
- **Wochenweises Durchgehen** – „Nächste Woche" schiebt den Zeitraum um genau
  sieben Tage weiter, ohne den Zuschnitt zu verändern.
- **Wochentag an jedem Datum** – zweibuchstabig (`Mo`, `Di`, … `So`) in
  Zeit-Display, Vorschau, Blacklist und Logbuch.
- **Blacklist pro Nutzer** – wirkt dauerhaft auf alle künftigen Zeitreisen.
  Bei Serien wird die ganze Serie ausgeschlossen (über `grandparentRatingKey`).
- **Eine feste Playlist pro Plex-Nutzer** – wird bei jeder Zeitreise geleert und
  chronologisch neu befüllt.
- **Watched-Status je Betrachter** – jede Zeitreise läuft im Kontext des
  jeweiligen Home-Users (siehe [Multi-User](#multi-user-und-watched-status)).
- **Automatisches Nachziehen** – periodisches Polling plus optionaler
  Plex-Webhook, damit gesehene Titel zeitnah aus der Playlist fallen.
- **Almanachs** – beliebig viele benannte Sammlungen aus gesuchten Serien und
  Filmen (z. B. je ein Franchise), jede mit eigener Playlist in Release-Order,
  jederzeit erweiterbar und ebenfalls automatisch nachgezogen.
- **Watch-Status zurücksetzen** – ein Almanach lässt sich komplett auf
  „ungesehen“ setzen, um ihn von vorn zu schauen (zweistufige Rückfrage).
- **Cover-Bilder** – jede Playlist (Zeitreise wie Almanach) bekommt auf Wunsch
  ein eigenes Poster in Plex.
- **Sammlungen gemeinsam nutzen** – ein Almanach lässt sich für weitere
  Home-User freigeben: ein Inhalt für alle, aber je Profil eine eigene Playlist
  mit dem eigenen Watch-Status.
- **Logbuch** – jeder Lauf mit Art, Zeitraum, Auslöser und Trefferzahl.

## Multi-User und Watched-Status

Plex-Playlists haben eine feste Item-Liste. Ob ein Item *enthalten* ist, wird
beim Bauen anhand des Unwatched-Status **eines** Accounts entschieden – das lässt
sich nicht dynamisch pro Betrachter umbiegen. Deshalb bekommt jede Person genau
eine dauerhafte Playlist:

```
Plex Time Machine – Alex
Plex Time Machine – Nina
```

Für Almanachs gilt dasselbe – jede Sammlung bekommt ihre eigene Playlist,
z. B. `Plex Almanach – Alex · Star Wars`.

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
| `PTM_ALMANACH_PLAYLIST_NAME_TEMPLATE` | `Plex Almanach – {user} · {name}` | Name der Almanach-Playlists (`{name}` = Name der Sammlung) |
| `PTM_POLL_INTERVAL_MINUTES` | `30` | Periodisches Nachziehen; `0` schaltet es ab |
| `PTM_WEBHOOK_DEBOUNCE_SECONDS` | `20` | Sammelfenster für Webhook-Events |
| `PTM_WEBHOOK_TOKEN` | – | Optionales Geheimnis für `/webhook/plex?token=…` |
| `PTM_DATABASE_URL` | `sqlite:///./data/plex_time_machine.db` | Speicherort der SQLite-DB |
| `PTM_COVER_DIR` | `./data/covers` | Ablage der Cover-Bilder |
| `PTM_COVER_MAX_BYTES` | `5242880` | Größtes erlaubtes Cover (5 MB) |
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

### Wenn nichts nachgezogen wird

Gesehene Titel verschwinden über zwei Wege: das Polling (`PTM_POLL_INTERVAL_MINUTES`,
erster Lauf eine Minute nach dem Start) und – mit Plex Pass – den Webhook. So
lässt sich prüfen, ob das greift:

- **Fußzeile**: zeigt „zuletzt HH:MM UTC“ und „nächster Lauf HH:MM UTC“.
- **Logbuch**: jeder automatische Lauf erscheint mit Auslöser `poll` bzw.
  `webhook`.
- **Container-Log**: `docker compose logs -f` zeigt Zeilen wie
  `[poll] Aktualisierung gestartet` und `[poll] Alex: 8 Items -> …`.

Zwei Dinge, die kein Fehler sind:

- **Angefangene, aber nicht zu Ende geschaute Titel** gelten in Plex als
  ungesehen und bleiben deshalb in der Playlist.
- Der Zeitreise-Zeitraum wird beim Polling unverändert übernommen; wer einen
  neuen Zeitraum will, wählt ihn im Cockpit.

## Bedienung

1. **Cockpit** – Zeitraum über die Datumsfelder, die Wochenschritte oder die
   Jahrzehnt-Presets wählen. Die Vorschau lädt live, ohne etwas in Plex zu
   verändern.
2. **Blacklist** – das ⊘-Symbol in einer Zeile schließt Film bzw. Serie dauerhaft
   aus; die Vorschau aktualisiert sich sofort. Verwalten und freigeben unter
   *Blacklist*.
3. **Zeitreise starten** – erst dieser Knopf schreibt die Auswahl in die feste
   Playlist des aktuellen Reisenden.
4. **Reisender** – der Umschalter oben wechselt den Home-User-Kontext
   (Vorschau, Blacklist, Playlist und Logbuch sind pro Nutzer getrennt).

### Almanachs: Serien und Filme gezielt sammeln

Der Reiter **Almanach** ist die zweite Betriebsart – statt eines Zeitraums
stellst du hier benannte Sammlungen zusammen. Davon darf es beliebig viele
geben (z. B. „Star Wars“, „Achtziger“, „Tatort-Klassiker“); jede bekommt ihre
eigene Playlist.

1. **Anlegen** – auf der Übersicht einen Namen eingeben. Die Übersicht zeigt zu
   jeder Sammlung Playlist-Name, letzten Lauf und Titelzahl.
2. **Suchen** – im Almanach einen Titel oder Teil davon eingeben (z. B.
   `Star Wars`); gefunden wird alles aus Film- und Serienbibliothek, dessen Name
   den Begriff enthält. Die Trefferliste lädt schon beim Tippen.
3. **Aufnehmen und entfernen** – das `+` legt Serie oder Film in den Bestand,
   das `✕` nimmt ihn wieder heraus. Bei Serien zählt die ganze Serie.
4. **Vorschau anzeigen** – zeigt, was in der Playlist landen würde: alle
   ungesehenen Episoden der gewählten Serien plus die gewählten Filme, streng
   nach Erscheinungsdatum sortiert (Release Order).
5. **Almanach erstellen** – schreibt das Ergebnis in die Playlist der Sammlung.

Ein Almanach lässt sich jederzeit wieder öffnen, **umbenennen** (die
Plex-Playlist wird mitbenannt) oder **löschen** (die Playlist verschwindet mit).

Der Bestand bleibt dauerhaft gespeichert. Polling und Webhook ziehen jede
Almanach-Playlist genauso nach wie die Zeitreise-Playlist – gesehene Folgen
fallen also von selbst heraus, und neu hinzugekommene Episoden einer
gesammelten Serie kommen automatisch dazu.

Zwei bewusste Festlegungen:

- **Die Blacklist gilt hier nicht.** Wer eine Serie ausdrücklich in einen
  Almanach legt, will sie sehen – die ausdrückliche Auswahl sticht.
- **Einträge, die aus der Bibliothek verschwinden**, werden beim Bauen
  übersprungen und im Ergebnis benannt, statt den ganzen Lauf abzubrechen.

### Cover für die Playlists

Sowohl die Zeitreise-Playlist (im Cockpit) als auch jeder Almanach haben einen
Abschnitt **Cover der Playlist**: Bild auswählen, „Cover setzen“ – fertig. Das
Bild wird als Poster an Plex übertragen und taucht dort überall auf, wo die
Playlist erscheint.

- Unterstützt werden JPEG, PNG, GIF und WebP bis 5 MB. Geprüft wird der
  Dateiinhalt, nicht die Endung; Plex stellt Poster hochkant (2:3) am besten dar.
- Das Bild wird lokal unter `PTM_COVER_DIR` abgelegt und **automatisch erneut
  übertragen**, wenn Plex die Playlist zwischendurch verwirft – das passiert,
  sobald alles gesehen ist und der nächste Lauf sie neu anlegt.
- Existiert die Playlist noch nicht, wird das Cover gemerkt und beim ersten Bau
  gesetzt.
- „Cover entfernen“ löscht die Datei und auch das Poster in Plex.

### Sammlungen gemeinsam nutzen

Im Abschnitt **Für andere Profile** einer Sammlung lassen sich weitere
Home-User freigeben. Freigeben legt **keine Kopie** an:

- **Der Inhalt bleibt einer.** Nimmt der Eigentümer später Titel auf oder
  heraus, gilt das sofort für alle freigegebenen Profile – beim nächsten Bau
  und beim Polling.
- **Der Fortschritt bleibt persönlich.** Jedes Profil bekommt seine eigene
  Playlist (`Plex Almanach – <Profil> · <Name>`), gefüllt mit dem, was *dieses*
  Profil noch nicht gesehen hat. Aus derselben Sammlung wird so bei einer Person
  eine Playlist mit vier, bei einer anderen mit sechs Titeln.
- **Zurücksetzen wirkt nur auf das eigene Profil**; die anderen behalten ihren
  Fortschritt.

Wer was darf:

| | Eigentümer | freigegebenes Profil |
|---|---|---|
| Inhalt suchen, aufnehmen, entfernen | ja | nein (sieht ihn nur) |
| Umbenennen, löschen, Cover setzen | ja | nein |
| Freigaben verwalten | ja | nein |
| Eigene Playlist bauen | ja | ja |
| Eigenen Watch-Status zurücksetzen | ja | ja |

Der „Erstellen“-Knopf baut beim Eigentümer die Playlists **aller** beteiligten
Profile (jeweils mit deren Watch-Status), bei einem freigegebenen Profil nur die
eigene. Eine zurückgenommene Freigabe entfernt auch die Playlist dieses Profils;
der Inhalt bleibt beim Eigentümer.

### Watch-Status zurücksetzen

Am Ende jedes Almanachs steht die **Gefahrenzone** mit „Watch-Status
zurücksetzen“. Damit gelten alle Filme und Episoden der Sammlung wieder als
ungesehen – praktisch, um ein Franchise von vorn zu schauen. Technisch ist das
`markUnplayed()` aus plexapi (`/:/unscrobble`), ausgeführt mit dem Token des
jeweiligen Home-Users; der Watch-Status anderer Nutzer bleibt unberührt.

Weil sich das nicht rückgängig machen lässt, sind zwei Bestätigungen nötig:

1. Der erste Klick zeigt nur eine Rückfrage – mit genauer Zahl, was betroffen
   wäre („2 gesehene Episoden und 2 gesehene Filme von insgesamt 5 Episoden und
   4 Filmen“), für welchen Plex-Nutzer, und was danach passiert.
2. Erst der rote Knopf „Ja, N Titel zurücksetzen“ führt es aus – und stellt
   davor noch eine letzte Sicherheitsabfrage.

Direkt danach wird die Playlist der Sammlung neu gebaut; sie enthält dann
wieder alle Titel.

### Wochenweise durch die Zeit

Für den typischen Ablauf „eine Woche nach der anderen“ gibt es drei Knöpfe:

| Knopf | Wirkung |
|---|---|
| **◀ Woche zurück** | Start *und* Ende sieben Tage früher |
| **Woche ausrichten** | setzt den Zeitraum auf Montag–Montag der Startwoche |
| **Nächste Woche ▶** | Start *und* Ende sieben Tage später |

Verschoben wird immer um exakt sieben Tage – die Länge des Zeitraums und die
Wochentage bleiben also erhalten. Aus `Mo 31.01.2000 – Mo 07.02.2000` wird
`Mo 07.02.2000 – Mo 14.02.2000`. Jeder Schritt speichert den neuen Zeitraum
sofort, sodass die nächste Sitzung dort weitermacht, wo die letzte aufgehört
hat.

Der Zuschnitt Montag-bis-Montag hat beide Ränder inklusive: ein Titel, der
genau auf den gemeinsamen Montag fällt, taucht in beiden Wochen auf. Wer das
nicht möchte, setzt das Ende einen Tag früher (Montag–Sonntag) – die
Wochenschritte übernehmen diesen Zuschnitt dann unverändert.

## Architektur

```
app/
├── main.py          FastAPI: Seiten, htmx-Fragmente, Webhook, Thumb-Proxy
├── almanach.py      Titelsuche, Sammlungen, Release-Order-Playlist, Reset, Freigaben
├── config.py        Settings aus Env-Variablen
├── covers.py        Cover-Bilder prüfen, ablegen, wiederfinden
├── formatting.py    Deutsche Datumsformate, Wochentage, Wochenrechnung
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
| `GET` | `/almanach` | Übersicht aller Sammlungen |
| `POST` | `/almanach/new` | Sammlung anlegen |
| `GET` | `/almanach/{id}` | Sammlung öffnen: Suche, Bestand, Ausgabe |
| `POST` | `/almanach/{id}/rename`, `/almanach/{id}/delete` | Umbenennen, löschen |
| `GET` | `/almanach/{id}/search`, `/almanach/{id}/preview` | Trefferliste, Vorschau in Release-Order |
| `POST` | `/almanach/{id}/add`, `/almanach/{id}/remove` | Bestand pflegen |
| `POST` | `/almanach/{id}/sync` | Playlist der Sammlung schreiben |
| `POST` | `/almanach/{id}/share`, `/almanach/{id}/share/revoke` | Freigaben erteilen und zurücknehmen |
| `POST` | `/almanach/{id}/cover`, `/almanach/{id}/cover/delete` | Cover setzen bzw. entfernen |
| `GET` | `/almanach/{id}/cover/image` | Cover ausliefern (für die Vorschau) |
| `POST` | `/cover/timemachine`, `/cover/timemachine/delete` | Cover der Zeitreise-Playlist |
| `GET`/`POST` | `/almanach/{id}/reset` | Watch-Status zurücksetzen (Rückfrage / Ausführung) |
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
Playlist selbst entfernt), Wochenrechnung und Wochentagsanzeige, den Almanach
(Titelsuche, Serien-Auflösung, Release-Order, fehlende Einträge), die Freigabe an andere
Profile inklusive getrennter Watch-Stände, Cover-Prüfung und -Übertragung sowie
Scheduler-Entprellung und alle HTTP-Endpunkte gegen ein Plex-Double ab –
ein echter Plex-Server wird dafür nicht gebraucht.

Bestehende Datenbanken werden beim Start automatisch um neue Spalten ergänzt;
ein Update kostet also keine Blacklist- oder Logbuch-Einträge. Ein Almanach aus
der Version vor den benannten Sammlungen wird dabei zu „Mein Almanach“ und
behält seinen bisherigen Playlist-Namen; Sammlungen aus der Version vor den
Freigaben bekommen automatisch eine Freigabe-Zeile für ihren Eigentümer und
behalten damit ihre bestehende Plex-Playlist.

## Sicherheitshinweis

Die App kennt keine eigene Anmeldung und geht von einem vertrauenswürdigen
Heimnetz aus. Wer sie ins Internet stellt, sollte einen Reverse-Proxy mit
Authentifizierung davorsetzen und `PTM_WEBHOOK_TOKEN` setzen.

## Offene Punkte

- Friends-Accounts (eigener Plex-Login) statt nur Home-User
- Blacklist auf Episoden-Ebene statt nur ganze Serien/Filme
- Almanach: freigegebene Profile dürfen den Inhalt bisher nicht mitpflegen
- Historie zuletzt genutzter Zeiträume als Schnellauswahl
  (aktuell wird nur der jeweils letzte Zeitraum gemerkt)
