# AGENTS.md

Hinweise für Coding-Agents, die an **Plex Time Machine** arbeiten. Was die
Software kann und wie man sie betreibt, steht in der [README](README.md) – hier
steht, wie man in diesem Repo *arbeitet*.

## Überblick

Selbst gehosteter Dienst, der für jeden Plex-Home-User genau eine dauerhafte
Playlist pflegt: alles, was in einem gewählten Zeitraum erschienen und noch
ungesehen ist. Dazu Almanachs (benannte Sammlungen in Release-Order),
Sendeplätze und optionale Übergangsclips.

FastAPI + Jinja2 + htmx (serverseitig gerendert, kein Build-Schritt),
SQLModel/SQLite, APScheduler, plexapi, Pillow + FFmpeg für die Clips.

```
app/
├── main.py            FastAPI: Seiten, htmx-Fragmente, Webhooks, Fehlerseite
├── config.py          Settings aus PTM_*-Umgebungsvariablen
├── db.py              Modelle, Migrationen, Repository-Funktionen
├── plex_client.py     plexapi-Wrapper inkl. Home-User-Impersonation
├── sync_engine.py     Suche, Blacklist, Sortierung, Playlist-Pflege
├── almanach.py        Sammlungen, Release-Order, Freigaben, Reset
├── slots.py           Sendeplätze: Zeiten lesen, Tagesreihenfolge
├── scheduler.py       Polling, Webhook-Entprellung, Clip-Erzeugung
├── transitions.py     Rendert die Übergangsclips (Pillow + FFmpeg)
├── transition_build.py  Clips planen, bauen, in Plex wiederfinden
├── covers.py          Cover prüfen, ablegen, übertragen
├── logbuffer.py       Ringpuffer der letzten Logzeilen für die Oberfläche
├── templates/         Jinja2
└── static/            Theme-CSS und lokal abgelegtes htmx
```

## Einrichten

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx          # nur für die Tests
```

Python 3.11. Laufen lassen: `uvicorn app.main:app --reload --port 8080`.
Ohne `PTM_PLEX_TOKEN` startet die App, bleibt aber ohne Daten – zum Entwickeln
reicht meist das Plex-Double aus den Tests.

Für die Übergangsclips zusätzlich `ffmpeg` im `PATH` (im Docker-Image über
`fonts-liberation`/`fonts-dejavu-core` und `ffmpeg` enthalten). Fehlt es,
überspringen die betroffenen Tests sich selbst.

## Tests

```bash
pytest -q                          # komplette Suite, wenige Sekunden
pytest tests/test_slots.py -q      # einzelne Datei
```

**Die Suite läuft ohne echten Plex-Server.** `tests/conftest.py` stellt ein
vollständiges Double bereit (`FakeServer`, `FakeGateway`, Bibliotheken,
Playlists, Übergangs-Bibliothek) plus die Fixtures `session`, `gateway`,
`client`. Neue Tests hängen sich dort ein, statt Netzzugriffe zu mocken.

Regeln, die sich hier bewährt haben:

- **Ein Regressionstest muss gegen das alte Verhalten fehlschlagen.** Erst den
  Fehler nachstellen, dann beheben, dann den Test kurz gegen den alten Stand
  laufen lassen. Ein Test, der vorher und nachher grün ist, prüft nichts.
- Testnamen sind englische Sätze (`test_a_late_slot_moves_the_series_behind_the_movie`),
  Docstrings erklären den Zweck auf Deutsch.
- Tests, die FFmpeg brauchen, tragen
  `@pytest.mark.skipif(ffmpeg_pfad() is None, ...)`.

## Stil

- **Bezeichner, Kommentare, Docstrings und Oberflächentexte auf Deutsch**,
  Testnamen auf Englisch. Das ist gewachsen und bleibt so.
- Kommentare erklären das **Warum**, nicht das Was – bevorzugt dort, wo etwas
  überrascht (eine Plex-Eigenheit, eine Reihenfolge, die Gründe hat).
- Type Hints überall, `from __future__ import annotations` am Dateikopf.
- Neue Abhängigkeiten nur, wenn es ohne wirklich nicht geht; die
  `requirements.txt` ist bewusst kurz und gepinnt.
- Keine Frontend-Toolchain. htmx liegt lokal unter `app/static/js/`.

## Fallstricke, die schon einmal wehgetan haben

Diese Punkte haben je einen Fehlerbericht gekostet. Wer sie kennt, spart sich den
zweiten.

- **Nie blockierendes plexapi im Event-Loop.** Jeder Plex-Aufruf gehört in
  `run_in_threadpool` (Web) bzw. `asyncio.to_thread` (Scheduler). Sonst steht
  bei langsamem Plex die komplette Oberfläche, nicht nur die eine Seite.
- **Transaktion vor Plex-Zugriffen freigeben** (`session.commit()`), sonst
  läuft jeder parallele Klick in `database is locked`. SQLite läuft im
  WAL-Modus mit 30 s Busy-Timeout – das ersetzt die Sorgfalt nicht.
- **Schemaänderungen nur additiv.** Bestehende Datenbanken müssen ein Update
  überleben: `db._add_missing_columns` ergänzt neue Spalten,
  `db._drop_dead_columns` entfernt alte Pflichtspalten ohne Entsprechung
  (vorher wandern ihre Werte per Datenmigration weiter). Jede Änderung am
  Schema braucht einen Test in `tests/test_migration.py`, der eine alte
  Datenbank nachbaut.
- **Datumsfilter mit `date.isoformat()`**, nie `strftime("%Y-%m-%d")`: bei
  Jahren unter 1000 fallen dort die führenden Nullen weg und Plex weist den
  Filter ab (stiller Vollscan).
- **plexapi kennt nur `>>` und `<<`** als Datumsoperatoren, beide randscharf
  exklusiv. Deshalb serverseitig um einen Tag weiten und clientseitig prüfen.
- **Plex' `unwatched`-Filter nicht blind glauben** – `is_unwatched()`
  clientseitig gegenprüfen, sonst bleiben gesehene Titel in der Playlist.
- **Eine leer geräumte Playlist löscht Plex von selbst.** Nach dem Leeren neu
  nachschlagen und gegebenenfalls neu anlegen; `PlaylistOutcome(created=…)`
  steuert danach das erneute Hochladen des Covers.
- **Der Scheduler rechnet in UTC.** Immer zeitzonenbewusste `datetime`-Objekte
  einplanen, sonst feuert ein Job um den Zeitzonenversatz versetzt.
- **Clips blockieren nie einen Sync.** Gerendert wird im Hintergrund; fehlt ein
  Clip, entsteht die Playlist eben ohne ihn.

## Nachsehen, statt raten

- Der Reiter **Protokoll** (`/logs`) zeigt die letzten Logzeilen der laufenden
  Instanz; unbehandelte Ausnahmen landen mit Aufrufstapel dort und als lesbare
  Fehlerseite im Browser.
- Das Statusfeld **Übergänge** im Cockpit prüft Profil, FFmpeg, Ordner und
  Plex-Bibliothek und zeigt den Fortschritt einer laufenden Erzeugung.

Wer eine Änderung an der Oberfläche macht, sollte sie einmal wirklich im
Browser gesehen haben – nicht nur die Tests grün.

## Commits

- Betreff auf Deutsch, im Imperativ, ohne Präfix wie `feat:`.
- Der Rumpf erklärt **warum** – bei einem Fehler gern mit der Meldung, die ihn
  verursacht hat, und der Beobachtung, die dahin geführt hat.
- Nur committen, was zur Änderung gehört. `data/` und `.env` sind über
  `.gitignore` ausgeschlossen und bleiben es.

## Grenzen

Die App kennt keine eigene Anmeldung und geht von einem vertrauenswürdigen
Heimnetz aus. Token und Geheimnisse stehen ausschließlich in der `.env` bzw. in
Umgebungsvariablen – niemals im Code, in Tests oder in Logzeilen.
