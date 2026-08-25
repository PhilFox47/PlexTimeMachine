"""FastAPI-App: Dashboard, Blacklist, Logbuch, Webhook."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app import __version__, covers, db
from app import almanach as almanach_lib
from app.config import get_settings
from app.formatting import format_date, format_datetime, format_period, week_of, weekday_short
from app.plex_client import HomeUser, PlexUnavailable, get_gateway
from app.scheduler import SyncScheduler, get_scheduler, set_scheduler
from app.sync_engine import (
    PreviewResult,
    SyncResult,
    build_preview,
    clear_cover,
    push_cover,
    sync_user,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("plex_time_machine")

USER_COOKIE = "ptm_user"
#: Plex-Webhook-Events, die einen Re-Sync rechtfertigen.
RELEVANT_WEBHOOK_EVENTS = {"media.scrobble", "media.rate", "library.new"}


def _check_data_dir() -> None:
    """Früh und verständlich meckern, wenn das Datenverzeichnis nicht taugt.

    Im Container gehört ``/app/data`` einem Volume vom Host. Ist das nicht
    beschreibbar, scheitert sonst erst SQLite mit einer kryptischen Meldung –
    und der Container startet in einer Schleife neu.
    """
    url = get_settings().database_url
    if not url.startswith("sqlite"):
        return
    pfad = Path(url.split("///", 1)[-1]).expanduser()
    if pfad.name == ":memory:":
        return
    verzeichnis = pfad.parent if pfad.parent != Path("") else Path(".")
    try:
        verzeichnis.mkdir(parents=True, exist_ok=True)
        probe = verzeichnis / ".schreibtest"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        log.error(
            "Datenverzeichnis '%s' ist nicht beschreibbar (%s). Im Container "
            "gehört es dem gemounteten Volume – bitte Rechte prüfen, z. B. "
            "'chown -R 1000:1000 ./data'.",
            verzeichnis,
            exc,
        )
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_data_dir()
    db.init_db()
    scheduler = SyncScheduler()
    scheduler.start()
    set_scheduler(scheduler)
    log.info("Plex Time Machine %s bereit", __version__)
    try:
        yield
    finally:
        scheduler.shutdown()
        set_scheduler(None)


app = FastAPI(title="Plex Time Machine", version=__version__, lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["version"] = __version__
templates.env.filters["de_date"] = format_date
templates.env.filters["de_datetime"] = format_datetime
templates.env.filters["de_period"] = lambda pair: format_period(*pair)
templates.env.filters["weekday"] = weekday_short


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def load_users() -> tuple[list[HomeUser], str]:
    """Home-User laden; bei Problemen leere Liste + Fehlertext."""
    try:
        return get_gateway().home_users(), ""
    except PlexUnavailable as exc:
        return [], str(exc)
    except Exception as exc:  # pragma: no cover - unerwartet
        log.exception("Home-User konnten nicht geladen werden")
        return [], f"Unerwarteter Fehler: {exc}"


def resolve_user(request: Request, users: list[HomeUser]) -> str:
    """Aktiver Nutzer: Cookie, sonst erster Home-User."""
    selected = request.cookies.get(USER_COOKIE, "")
    known = {u.id for u in users}
    if selected and (selected in known or not users):
        return selected
    return users[0].id if users else ""


#: Vor 1870 gibt es keine Filme. Ein früheres Jahr ist praktisch immer ein
#: Vertipper im Datumsfeld – und würde als Zeitraum nur Vollscans auslösen.
MIN_YEAR = 1870


def is_plausible(day: Optional[date]) -> bool:
    return day is not None and day.year >= MIN_YEAR


def parse_period(start: str, end: str) -> tuple[Optional[date], Optional[date], str]:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError):
        return None, None, "Bitte ein gültiges Start- und Enddatum wählen."
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    if not (is_plausible(start_date) and is_plausible(end_date)):
        return None, None, (
            f"Das Jahr muss mindestens {MIN_YEAR} sein – bitte das Datum prüfen."
        )
    return start_date, end_date, ""


async def _read_cover(upload: UploadFile) -> bytes:
    """Hochgeladene Datei einlesen – ein Byte über dem Limit reicht zum Abbruch."""
    limit = get_settings().cover_max_bytes
    data = await upload.read(limit + 1)
    await upload.close()
    return data


def _store_cover(stem: str, data: bytes) -> tuple[Optional[str], str]:
    """(Dateiname, Fehlertext) – genau eines von beidem ist gesetzt."""
    try:
        return covers.store(stem, data), ""
    except covers.CoverError as exc:
        return None, str(exc)


def _cover_redirect(target: str, status: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("cover", status), ("cover_error", error)) if v})
    return RedirectResponse(f"{target}?{query}" if query else target, status_code=303)


def _push_cover_quietly(user_id: str, playlist_name: str, cover_path: str) -> str:
    """Cover sofort übertragen. Rückgabe: Statuscode für die Rückmeldung."""
    try:
        if push_cover(get_gateway(), user_id, playlist_name, cover_path):
            return "uebertragen"
        return "gespeichert"  # Playlist existiert noch nicht
    except Exception as exc:  # pragma: no cover - Plex kann offline sein
        log.warning("Cover für »%s« nicht übertragen: %s", playlist_name, exc)
        return "gespeichert"


def _clear_cover_quietly(user_id: str, playlist_name: str) -> None:
    try:
        clear_cover(get_gateway(), user_id, playlist_name)
    except Exception as exc:  # pragma: no cover - Plex kann offline sein
        log.warning("Poster von »%s« nicht entfernt: %s", playlist_name, exc)


def _serve_cover(filename: Optional[str]) -> Response:
    path = covers.path_for(filename)
    if path is None:
        return Response(status_code=404)
    return FileResponse(
        path,
        media_type=covers.content_type_for(path.name),
        headers={"Cache-Control": "no-store"},
    )


def _as_year(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_period() -> tuple[date, date]:
    """Vorbelegung für Erstnutzer: die Woche vor 40 Jahren.

    Sobald einmal ein Zeitraum gespeichert wurde, gewinnt immer der gemerkte
    Zeitraum aus dem ``UserState``.
    """
    return week_of(date.today() - timedelta(days=365 * 40))


async def page_context(
    request: Request, session: Session, *, preview=None, status=None
) -> dict:
    """Seitenkontext bauen – der Plex-Zugriff läuft dabei im Worker-Thread.

    Blockierende plexapi-Aufrufe dürfen nie im Event-Loop landen: ein langsamer
    oder nicht erreichbarer Plex-Server würde sonst den kompletten Webserver
    anhalten, nicht nur die betroffene Seite.
    """
    users, user_error = await run_in_threadpool(load_users)
    return dashboard_context(
        request, session, users, user_error, preview=preview, status=status
    )


def dashboard_context(
    request: Request,
    session: Session,
    users: list[HomeUser],
    user_error: str,
    *,
    preview=None,
    status=None,
) -> dict:
    user_id = resolve_user(request, users)
    state = db.get_or_create_user_state(session, user_id) if user_id else None

    start, end = default_period()
    period_warning = ""
    if state and state.has_period:
        if is_plausible(state.current_date_start) and is_plausible(state.current_date_end):
            start, end = state.current_date_start, state.current_date_end
        else:
            # Kann durch einen Vertipper im Datumsfeld entstanden sein. Statt
            # damit weiterzurechnen: Vorschlag zeigen und Bescheid sagen.
            period_warning = (
                f"Der gespeicherte Zeitraum "
                f"({state.current_date_start} – {state.current_date_end}) ist "
                f"unbrauchbar; hier steht ein Vorschlag. Bitte neu wählen."
            )

    scheduler = get_scheduler()
    return {
        "request": request,
        "settings": get_settings(),
        "users": users,
        "user_error": user_error,
        "current_user": user_id,
        "state": state,
        "start": start,
        "end": end,
        "period_warning": period_warning,
        "preview": preview,
        "status": status,
        "blacklist": db.list_blacklist(session, user_id) if user_id else [],
        "almanachs": db.list_almanachs(session, user_id) if user_id else [],
        "journeys": db.list_journeys(session, user_id) if user_id else [],
        "next_poll_at": scheduler.next_poll_at if scheduler else None,
        "last_poll_at": scheduler.last_poll_at if scheduler else None,
    }


async def compute_preview(session: Session, user_id: str, start: date, end: date):
    return await run_in_threadpool(build_preview, session, user_id, start, end)


def preview_response(request: Request, preview) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/preview.html", {"preview": preview}
    )


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(db.get_session)):
    """Cockpit ausliefern – die Vorschau holt sich die Seite selbst nach.

    Früher wurde sie hier berechnet; bei großen Bibliotheken hing der
    Seitenaufbau dann sekundenlang an Plex und wirkte wie eine tote Seite.
    """
    return templates.TemplateResponse(
        request, "dashboard.html", await page_context(request, session)
    )


@app.get("/blacklist", response_class=HTMLResponse)
async def blacklist_page(request: Request, session: Session = Depends(db.get_session)):
    return templates.TemplateResponse(
        request, "blacklist.html", await page_context(request, session)
    )


@app.get("/logbook", response_class=HTMLResponse)
async def logbook_page(request: Request, session: Session = Depends(db.get_session)):
    return templates.TemplateResponse(
        request, "logbook.html", await page_context(request, session)
    )


@app.post("/user/select")
async def select_user(user: str = Form(...)):
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(USER_COOKIE, user, max_age=60 * 60 * 24 * 365, httponly=True)
    return response


# ---------------------------------------------------------------------------
# Zeitraum + Vorschau (htmx)
# ---------------------------------------------------------------------------


@app.post("/period", response_class=HTMLResponse)
async def set_period(
    request: Request,
    start: str = Form(...),
    end: str = Form(...),
    session: Session = Depends(db.get_session),
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    start_date, end_date, error = parse_period(start, end)
    if error:
        return preview_response(request, PreviewResult(error=error))

    db.set_period(session, user_id, start_date, end_date)
    preview = await compute_preview(session, user_id, start_date, end_date)
    return preview_response(request, preview)


@app.get("/preview", response_class=HTMLResponse)
async def preview_fragment(
    request: Request,
    start: str = Query(...),
    end: str = Query(...),
    session: Session = Depends(db.get_session),
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    start_date, end_date, error = parse_period(start, end)
    if error:
        return preview_response(request, PreviewResult(error=error))
    preview = await compute_preview(session, user_id, start_date, end_date)
    return preview_response(request, preview)


# ---------------------------------------------------------------------------
# Blacklist (htmx)
# ---------------------------------------------------------------------------


@app.post("/blacklist/add", response_class=HTMLResponse)
async def blacklist_add(
    request: Request,
    rating_key: str = Form(...),
    media_type: str = Form("movie"),
    title: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    session: Session = Depends(db.get_session),
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    db.add_to_blacklist(session, user_id, rating_key, media_type, title)

    start_date, end_date, error = parse_period(start, end)
    if error:
        state = db.get_or_create_user_state(session, user_id)
        start_date, end_date = state.current_date_start, state.current_date_end
    if not (start_date and end_date):
        return HTMLResponse("")
    preview = await compute_preview(session, user_id, start_date, end_date)
    return preview_response(request, preview)


@app.post("/blacklist/remove")
async def blacklist_remove(
    request: Request,
    rating_key: str = Form(...),
    session: Session = Depends(db.get_session),
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    db.remove_from_blacklist(session, user_id, rating_key)
    return RedirectResponse("/blacklist", status_code=303)


# ---------------------------------------------------------------------------
# Zeitreise starten
# ---------------------------------------------------------------------------


@app.post("/sync", response_class=HTMLResponse)
async def start_journey(request: Request, session: Session = Depends(db.get_session)):
    users, user_error = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    if not user_id:
        result = SyncResult(user_id="", error=user_error or "Kein Nutzer gewählt.")
    else:
        result = await run_in_threadpool(sync_user, session, user_id, "manual")
    return templates.TemplateResponse(request, "partials/status.html", {"status": result})


# ---------------------------------------------------------------------------
# Cover der Zeitreise-Playlist
# ---------------------------------------------------------------------------


@app.post("/cover/timemachine")
async def timemachine_cover_upload(
    request: Request,
    cover: UploadFile = File(...),
    session: Session = Depends(db.get_session),
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    if not user_id:
        return _cover_redirect("/", error="Kein Nutzer gewählt.")

    state = db.get_or_create_user_state(session, user_id)
    filename, error = _store_cover(covers.timemachine_stem(user_id), await _read_cover(cover))
    if error:
        return _cover_redirect("/", error=error)

    state.cover_path = filename
    state.cover_applied_at = None
    session.add(state)
    session.commit()

    status = await run_in_threadpool(
        _push_cover_quietly, user_id, state.target_playlist_name, filename
    )
    if status == "uebertragen":
        state.cover_applied_at = db.utcnow()
        session.add(state)
        session.commit()
    return _cover_redirect("/", status=status)


@app.post("/cover/timemachine/delete")
async def timemachine_cover_delete(
    request: Request, session: Session = Depends(db.get_session)
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    if not user_id:
        return _cover_redirect("/")

    state = db.get_or_create_user_state(session, user_id)
    covers.remove(covers.timemachine_stem(user_id))
    state.cover_path = None
    state.cover_applied_at = None
    session.add(state)
    session.commit()
    await run_in_threadpool(_clear_cover_quietly, user_id, state.target_playlist_name)
    return _cover_redirect("/", status="entfernt")


@app.get("/cover/timemachine/image")
async def timemachine_cover_image(
    request: Request, session: Session = Depends(db.get_session)
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    if not user_id:
        return Response(status_code=404)
    return _serve_cover(db.get_or_create_user_state(session, user_id).cover_path)


# ---------------------------------------------------------------------------
# Almanach
# ---------------------------------------------------------------------------


async def _require_access(request: Request, session: Session, almanach_id: int):
    """Sammlung + Freigabe des aktuellen Profils laden – sonst 404."""
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    almanach = db.get_almanach(session, user_id, almanach_id) if user_id else None
    if almanach is None:
        raise HTTPException(status_code=404, detail="Almanach nicht gefunden")
    return almanach, db.get_or_create_share(session, almanach, user_id), user_id


async def _require_owner(request: Request, session: Session, almanach_id: int):
    """Wie _require_access, aber nur für den Eigentümer – er pflegt den Inhalt."""
    almanach, share, user_id = await _require_access(request, session, almanach_id)
    if almanach.plex_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Nur der Eigentümer der Sammlung kann sie ändern.",
        )
    return almanach, share, user_id


async def _detail_context(request: Request, session: Session, almanach, share, user_id) -> dict:
    context = await page_context(request, session)
    context["almanach"] = almanach
    context["share"] = share
    context["entries"] = db.list_almanach_entries(session, almanach.id)
    context["is_owner"] = almanach.plex_user_id == user_id
    context["shares"] = db.list_shares(session, almanach.id)
    context["share_targets"] = _share_targets(session, almanach, context["users"])
    return context


def _share_targets(session: Session, almanach, users) -> list[dict]:
    """Andere Home-User samt Hinweis, ob die Sammlung für sie freigegeben ist."""
    freigegeben = db.share_user_ids(session, almanach.id)
    return [
        {"user": user, "shared": user.id in freigegeben}
        for user in users
        if user.id != almanach.plex_user_id
    ]


@app.get("/almanach", response_class=HTMLResponse)
async def almanach_overview(request: Request, session: Session = Depends(db.get_session)):
    context = await page_context(request, session)
    context["shares_by_almanach"] = {
        almanach.id: db.get_or_create_share(session, almanach, context["current_user"])
        for almanach in context["almanachs"]
    }
    return templates.TemplateResponse(request, "almanach.html", context)


@app.post("/almanach/new")
async def almanach_new(
    request: Request,
    name: str = Form(...),
    session: Session = Depends(db.get_session),
):
    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)
    if not user_id:
        return RedirectResponse("/almanach", status_code=303)
    almanach = db.create_almanach(session, user_id, name)
    return RedirectResponse(f"/almanach/{almanach.id}", status_code=303)


@app.get("/almanach/{almanach_id}", response_class=HTMLResponse)
async def almanach_detail(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach, share, user_id = await _require_access(request, session, almanach_id)
    return templates.TemplateResponse(
        request,
        "almanach_detail.html",
        await _detail_context(request, session, almanach, share, user_id),
    )


@app.post("/almanach/{almanach_id}/rename")
async def almanach_rename(
    request: Request,
    almanach_id: int,
    name: str = Form(...),
    session: Session = Depends(db.get_session),
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    # Playlist-Namen vorher merken, damit die Playlists in Plex mitwandern.
    vorher = {s.plex_user_id: s.target_playlist_name for s in db.list_shares(session, almanach.id)}
    db.rename_almanach(session, almanach, name)
    await run_in_threadpool(_rename_playlists_quietly, session, almanach, vorher)
    return RedirectResponse(f"/almanach/{almanach_id}", status_code=303)


def _rename_playlists_quietly(session: Session, almanach, vorher: dict) -> None:
    """Plex-Playlists aller Profile nachbenennen; Fehler sind nicht kritisch."""
    for share in db.list_shares(session, almanach.id):
        old = vorher.get(share.plex_user_id, "")
        try:
            almanach_lib.rename_playlist(share, old)
        except Exception as exc:  # pragma: no cover - Plex kann offline sein
            log.warning("Playlist »%s« nicht umbenannt: %s", old, exc)


@app.post("/almanach/{almanach_id}/delete")
async def almanach_delete(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    await run_in_threadpool(_delete_playlists_quietly, session, almanach)
    db.delete_almanach(session, almanach)
    return RedirectResponse("/almanach", status_code=303)


def _delete_playlists_quietly(session: Session, almanach) -> None:
    """Die Playlists aller Profile mit entfernen – Fehler sind nicht kritisch."""
    for share in db.list_shares(session, almanach.id):
        try:
            almanach_lib.delete_playlist(share)
        except Exception as exc:  # pragma: no cover - Plex kann offline sein
            log.warning("Playlist zu »%s« nicht entfernt: %s", almanach.name, exc)


@app.get("/almanach/{almanach_id}/search", response_class=HTMLResponse)
async def almanach_search(
    request: Request,
    almanach_id: int,
    q: str = Query(""),
    session: Session = Depends(db.get_session),
):
    almanach, _share, user_id = await _require_owner(request, session, almanach_id)
    result = await run_in_threadpool(
        almanach_lib.search_titles, session, almanach, q, user_id
    )
    return templates.TemplateResponse(
        request, "partials/almanach_search.html", {"search": result, "almanach": almanach}
    )


def _stock_response(request: Request, session: Session, almanach, is_owner: bool = True) -> HTMLResponse:
    """Bestandsliste neu rendern – reine Datenbankarbeit, daher schnell."""
    return templates.TemplateResponse(
        request,
        "partials/almanach_stock.html",
        {
            "almanach": almanach,
            "entries": db.list_almanach_entries(session, almanach.id),
            "is_owner": is_owner,
            "shares": db.list_shares(session, almanach.id),
        },
    )


@app.post("/almanach/{almanach_id}/add", response_class=HTMLResponse)
async def almanach_add(
    request: Request,
    almanach_id: int,
    rating_key: str = Form(...),
    media_type: str = Form("movie"),
    title: str = Form(""),
    year: str = Form(""),
    session: Session = Depends(db.get_session),
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    db.add_to_almanach(session, almanach, rating_key, media_type, title, _as_year(year))
    return _stock_response(request, session, almanach)


@app.post("/almanach/{almanach_id}/remove", response_class=HTMLResponse)
async def almanach_remove(
    request: Request,
    almanach_id: int,
    rating_key: str = Form(...),
    session: Session = Depends(db.get_session),
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    db.remove_from_almanach(session, almanach, rating_key)
    return _stock_response(request, session, almanach)


@app.get("/almanach/{almanach_id}/preview", response_class=HTMLResponse)
async def almanach_preview(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    _almanach, share, _user = await _require_access(request, session, almanach_id)
    preview = await run_in_threadpool(almanach_lib.build_preview, session, share)
    return templates.TemplateResponse(
        request, "partials/almanach_preview.html", {"preview": preview}
    )


@app.post("/almanach/{almanach_id}/sync", response_class=HTMLResponse)
async def almanach_sync(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    """Der Eigentümer baut für alle Profile, ein Gast nur für sich."""
    almanach, share, user_id = await _require_access(request, session, almanach_id)
    if almanach.plex_user_id == user_id:
        results = await run_in_threadpool(
            almanach_lib.sync_collection, session, almanach, "manual"
        )
    else:
        results = [
            await run_in_threadpool(almanach_lib.sync_share, session, share, "manual")
        ]
    return templates.TemplateResponse(
        request, "partials/almanach_sync.html", {"results": results}
    )


@app.post("/almanach/{almanach_id}/share", response_class=HTMLResponse)
async def almanach_share(
    request: Request,
    almanach_id: int,
    profiles: list[str] = Form(default=[]),
    session: Session = Depends(db.get_session),
):
    """Sammlung für andere Profile freigeben und dort gleich bauen."""
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    users, _ = await run_in_threadpool(load_users)
    known = {user.id for user in users}
    targets = [
        name for name in profiles if name in known and name != almanach.plex_user_id
    ]

    results = []
    if targets:
        results = await run_in_threadpool(
            almanach_lib.share_with_users, session, almanach, targets
        )

    return templates.TemplateResponse(
        request,
        "partials/almanach_share.html",
        {
            "almanach": almanach,
            "share_targets": _share_targets(session, almanach, users),
            "share_results": results,
            "share_message": "" if targets else "Kein Profil gewählt.",
            "is_owner": True,
        },
    )


@app.post("/almanach/{almanach_id}/share/revoke", response_class=HTMLResponse)
async def almanach_share_revoke(
    request: Request,
    almanach_id: int,
    profile: str = Form(...),
    session: Session = Depends(db.get_session),
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    removed = await run_in_threadpool(
        almanach_lib.revoke_share, session, almanach, profile
    )
    users, _ = await run_in_threadpool(load_users)
    return templates.TemplateResponse(
        request,
        "partials/almanach_share.html",
        {
            "almanach": almanach,
            "share_targets": _share_targets(session, almanach, users),
            "share_results": [],
            "share_message": f"Freigabe für {profile} zurückgenommen."
            if removed
            else "Freigabe war nicht vorhanden.",
            "is_owner": True,
        },
    )


# --- Cover der Almanach-Playlist -----------------------------------------


@app.post("/almanach/{almanach_id}/cover")
async def almanach_cover_upload(
    request: Request,
    almanach_id: int,
    cover: UploadFile = File(...),
    session: Session = Depends(db.get_session),
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    target = f"/almanach/{almanach_id}"

    filename, error = _store_cover(
        covers.almanach_stem(almanach.id), await _read_cover(cover)
    )
    if error:
        return _cover_redirect(target, error=error)

    almanach.cover_path = filename
    session.add(almanach)
    session.commit()
    db.reset_cover_state(session, almanach.id)

    status = await run_in_threadpool(_push_cover_to_shares, session, almanach, filename)
    return _cover_redirect(target, status=status)


def _push_cover_to_shares(session: Session, almanach, filename: str) -> str:
    """Cover auf die Playlists aller Profile übertragen."""
    reached = 0
    for share in db.list_shares(session, almanach.id):
        try:
            if push_cover(
                get_gateway(), share.plex_user_id, share.target_playlist_name, filename
            ):
                share.cover_applied_at = db.utcnow()
                session.add(share)
                reached += 1
        except Exception as exc:  # pragma: no cover - Plex kann offline sein
            log.warning("Cover für %s nicht übertragen: %s", share.plex_user_id, exc)
    session.commit()
    return "uebertragen" if reached else "gespeichert"


@app.post("/almanach/{almanach_id}/cover/delete")
async def almanach_cover_delete(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach, _share, _user = await _require_owner(request, session, almanach_id)
    covers.remove(covers.almanach_stem(almanach.id))
    almanach.cover_path = None
    session.add(almanach)
    session.commit()
    db.reset_cover_state(session, almanach.id)
    await run_in_threadpool(_clear_cover_from_shares, session, almanach)
    return _cover_redirect(f"/almanach/{almanach_id}", status="entfernt")


def _clear_cover_from_shares(session: Session, almanach) -> None:
    for share in db.list_shares(session, almanach.id):
        _clear_cover_quietly(share.plex_user_id, share.target_playlist_name)


@app.get("/almanach/{almanach_id}/cover/image")
async def almanach_cover_image(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach, _share, _user = await _require_access(request, session, almanach_id)
    return _serve_cover(almanach.cover_path)


# --- Watch-Status zurücksetzen: erst zeigen, dann bestätigen --------------


@app.get("/almanach/{almanach_id}/reset", response_class=HTMLResponse)
async def almanach_reset_confirm(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    """Erste Stufe: zeigt genau, was für dieses Profil zurückgesetzt würde."""
    almanach, share, _user = await _require_access(request, session, almanach_id)
    plan = await run_in_threadpool(
        almanach_lib.plan_reset, session, share, None, almanach.name
    )
    return templates.TemplateResponse(
        request, "partials/almanach_reset.html", {"plan": plan, "almanach": almanach}
    )


@app.post("/almanach/{almanach_id}/reset", response_class=HTMLResponse)
async def almanach_reset(
    request: Request,
    almanach_id: int,
    confirm: str = Form(""),
    session: Session = Depends(db.get_session),
):
    """Zweite Stufe: führt den Reset aus und baut die eigene Playlist neu."""
    almanach, share, _user = await _require_access(request, session, almanach_id)
    if confirm != "ja":
        plan = await run_in_threadpool(
            almanach_lib.plan_reset, session, share, None, almanach.name
        )
        return templates.TemplateResponse(
            request, "partials/almanach_reset.html", {"plan": plan, "almanach": almanach}
        )

    result = await run_in_threadpool(
        almanach_lib.reset_watch_state, session, share, None, almanach.name
    )
    sync = None
    if result.ok:
        sync = await run_in_threadpool(almanach_lib.sync_share, session, share, "reset")
    return templates.TemplateResponse(
        request,
        "partials/almanach_reset_done.html",
        {"result": result, "sync": sync, "almanach": almanach},
    )


# ---------------------------------------------------------------------------
# Webhook + Service-Endpunkte
# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
# Webhook + Service-Endpunkte
# ---------------------------------------------------------------------------


@app.post("/webhook/plex")
async def plex_webhook(request: Request, token: str = Query("")):
    settings = get_settings()
    if settings.webhook_token and token != settings.webhook_token:
        return JSONResponse({"detail": "invalid token"}, status_code=401)

    event = ""
    try:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form = await request.form()
            payload = json.loads(str(form.get("payload", "{}")))
        else:
            body = await request.body()
            payload = json.loads(body or b"{}")
        event = payload.get("event", "")
    except (ValueError, KeyError) as exc:
        log.warning("Webhook-Payload nicht lesbar: %s", exc)
        return JSONResponse({"status": "ignored", "reason": "unparsable"})

    if event not in RELEVANT_WEBHOOK_EVENTS:
        return JSONResponse({"status": "ignored", "event": event})

    scheduler = get_scheduler()
    if scheduler is None:
        return JSONResponse({"status": "ignored", "reason": "scheduler inaktiv"})

    run_at = scheduler.request_webhook_sync()
    log.info("Webhook '%s' empfangen – Sync geplant für %s", event, run_at)
    return JSONResponse({"status": "scheduled", "event": event, "run_at": run_at.isoformat()})


#: Nur Bildpfade der Plex-Bibliothek dürfen über den Proxy geladen werden.
THUMB_PREFIXES = ("/library/", "/photo/")


@app.get("/thumb")
async def thumb_proxy(request: Request, path: str = Query(...)):
    """Poster durchreichen, damit kein Plex-Token im Browser landet."""
    if path.startswith("//") or "://" in path or not path.startswith(THUMB_PREFIXES):
        return Response(status_code=400)

    users, _ = await run_in_threadpool(load_users)
    user_id = resolve_user(request, users)

    def fetch() -> tuple[int, bytes, str]:
        server = get_gateway().connect_as(user_id)
        url = server.url(path, includeToken=True)
        resp = requests.get(url, timeout=10)
        return resp.status_code, resp.content, resp.headers.get("content-type", "image/jpeg")

    try:
        status, content, content_type = await run_in_threadpool(fetch)
    except (PlexUnavailable, requests.RequestException) as exc:
        log.debug("Thumb '%s' nicht ladbar: %s", path, exc)
        return Response(status_code=502)

    if status != 200:
        return Response(status_code=status)
    return Response(
        content, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"}
    )


@app.get("/healthz")
async def healthz():
    settings = get_settings()
    scheduler = get_scheduler()
    return {
        "status": "ok",
        "version": __version__,
        "plex_configured": settings.configured,
        "poll_interval_minutes": settings.poll_interval_minutes,
        "next_poll_at": scheduler.next_poll_at.isoformat()
        if scheduler and scheduler.next_poll_at
        else None,
    }
