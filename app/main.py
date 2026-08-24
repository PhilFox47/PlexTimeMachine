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


@asynccontextmanager
async def lifespan(app: FastAPI):
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


def parse_period(start: str, end: str) -> tuple[Optional[date], Optional[date], str]:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError):
        return None, None, "Bitte ein gültiges Start- und Enddatum wählen."
    if end_date < start_date:
        start_date, end_date = end_date, start_date
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


def dashboard_context(
    request: Request, session: Session, *, preview=None, status=None
) -> dict:
    users, user_error = load_users()
    user_id = resolve_user(request, users)
    state = db.get_or_create_user_state(session, user_id) if user_id else None

    start, end = default_period()
    if state and state.has_period:
        start, end = state.current_date_start, state.current_date_end

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
        "preview": preview,
        "status": status,
        "blacklist": db.list_blacklist(session, user_id) if user_id else [],
        "almanachs": db.list_almanachs(session, user_id) if user_id else [],
        "journeys": db.list_journeys(session, user_id) if user_id else [],
        "next_poll_at": scheduler.next_poll_at if scheduler else None,
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
    context = dashboard_context(request, session)
    if context["current_user"] and context["state"] and context["state"].has_period:
        context["preview"] = await compute_preview(
            session, context["current_user"], context["start"], context["end"]
        )
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/blacklist", response_class=HTMLResponse)
async def blacklist_page(request: Request, session: Session = Depends(db.get_session)):
    return templates.TemplateResponse(
        request, "blacklist.html", dashboard_context(request, session)
    )


@app.get("/logbook", response_class=HTMLResponse)
async def logbook_page(request: Request, session: Session = Depends(db.get_session)):
    return templates.TemplateResponse(
        request, "logbook.html", dashboard_context(request, session)
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
    users, _ = load_users()
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
    users, _ = load_users()
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
    users, _ = load_users()
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
    users, _ = load_users()
    user_id = resolve_user(request, users)
    db.remove_from_blacklist(session, user_id, rating_key)
    return RedirectResponse("/blacklist", status_code=303)


# ---------------------------------------------------------------------------
# Zeitreise starten
# ---------------------------------------------------------------------------


@app.post("/sync", response_class=HTMLResponse)
async def start_journey(request: Request, session: Session = Depends(db.get_session)):
    users, user_error = load_users()
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
    users, _ = load_users()
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
    users, _ = load_users()
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
    users, _ = load_users()
    user_id = resolve_user(request, users)
    if not user_id:
        return Response(status_code=404)
    return _serve_cover(db.get_or_create_user_state(session, user_id).cover_path)


# ---------------------------------------------------------------------------
# Almanach
# ---------------------------------------------------------------------------


def _require_almanach(request: Request, session: Session, almanach_id: int):
    """Almanach des aktuell gewählten Nutzers laden – sonst 404."""
    users, _ = load_users()
    user_id = resolve_user(request, users)
    almanach = db.get_almanach(session, user_id, almanach_id) if user_id else None
    if almanach is None:
        raise HTTPException(status_code=404, detail="Almanach nicht gefunden")
    return almanach


def _detail_context(request: Request, session: Session, almanach) -> dict:
    context = dashboard_context(request, session)
    context["almanach"] = almanach
    context["entries"] = db.list_almanach_entries(session, almanach.id)
    return context


@app.get("/almanach", response_class=HTMLResponse)
async def almanach_overview(request: Request, session: Session = Depends(db.get_session)):
    return templates.TemplateResponse(
        request, "almanach.html", dashboard_context(request, session)
    )


@app.post("/almanach/new")
async def almanach_new(
    request: Request,
    name: str = Form(...),
    session: Session = Depends(db.get_session),
):
    users, _ = load_users()
    user_id = resolve_user(request, users)
    if not user_id:
        return RedirectResponse("/almanach", status_code=303)
    almanach = db.create_almanach(session, user_id, name)
    return RedirectResponse(f"/almanach/{almanach.id}", status_code=303)


@app.get("/almanach/{almanach_id}", response_class=HTMLResponse)
async def almanach_detail(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach = _require_almanach(request, session, almanach_id)
    return templates.TemplateResponse(
        request, "almanach_detail.html", _detail_context(request, session, almanach)
    )


@app.post("/almanach/{almanach_id}/rename")
async def almanach_rename(
    request: Request,
    almanach_id: int,
    name: str = Form(...),
    session: Session = Depends(db.get_session),
):
    almanach = _require_almanach(request, session, almanach_id)
    old_playlist_name = almanach.target_playlist_name
    db.rename_almanach(session, almanach, name)
    await run_in_threadpool(_rename_playlist_quietly, almanach, old_playlist_name)
    return RedirectResponse(f"/almanach/{almanach_id}", status_code=303)


def _rename_playlist_quietly(almanach, old_playlist_name: str) -> None:
    """Plex-Playlist nachbenennen; scheitert das, baut der nächste Sync sie neu."""
    try:
        almanach_lib.rename_playlist(almanach, old_playlist_name)
    except Exception as exc:  # pragma: no cover - Plex kann offline sein
        log.warning("Playlist »%s« nicht umbenannt: %s", old_playlist_name, exc)


@app.post("/almanach/{almanach_id}/delete")
async def almanach_delete(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach = _require_almanach(request, session, almanach_id)
    await run_in_threadpool(_delete_playlist_quietly, almanach)
    db.delete_almanach(session, almanach)
    return RedirectResponse("/almanach", status_code=303)


def _delete_playlist_quietly(almanach) -> None:
    """Zugehörige Plex-Playlist mit entfernen – Fehler sind nicht kritisch."""
    try:
        almanach_lib.delete_playlist(almanach)
    except Exception as exc:  # pragma: no cover - Plex kann offline sein
        log.warning("Playlist zu »%s« nicht entfernt: %s", almanach.name, exc)


@app.get("/almanach/{almanach_id}/search", response_class=HTMLResponse)
async def almanach_search(
    request: Request,
    almanach_id: int,
    q: str = Query(""),
    session: Session = Depends(db.get_session),
):
    almanach = _require_almanach(request, session, almanach_id)
    result = await run_in_threadpool(almanach_lib.search_titles, session, almanach, q)
    return templates.TemplateResponse(
        request, "partials/almanach_search.html", {"search": result, "almanach": almanach}
    )


def _stock_response(request: Request, session: Session, almanach) -> HTMLResponse:
    """Bestandsliste neu rendern – reine Datenbankarbeit, daher schnell."""
    return templates.TemplateResponse(
        request,
        "partials/almanach_stock.html",
        {
            "almanach": almanach,
            "entries": db.list_almanach_entries(session, almanach.id),
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
    almanach = _require_almanach(request, session, almanach_id)
    db.add_to_almanach(session, almanach, rating_key, media_type, title, _as_year(year))
    return _stock_response(request, session, almanach)


@app.post("/almanach/{almanach_id}/remove", response_class=HTMLResponse)
async def almanach_remove(
    request: Request,
    almanach_id: int,
    rating_key: str = Form(...),
    session: Session = Depends(db.get_session),
):
    almanach = _require_almanach(request, session, almanach_id)
    db.remove_from_almanach(session, almanach, rating_key)
    return _stock_response(request, session, almanach)


@app.get("/almanach/{almanach_id}/preview", response_class=HTMLResponse)
async def almanach_preview(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach = _require_almanach(request, session, almanach_id)
    preview = await run_in_threadpool(almanach_lib.build_preview, session, almanach)
    return templates.TemplateResponse(
        request, "partials/almanach_preview.html", {"preview": preview}
    )


@app.post("/almanach/{almanach_id}/sync", response_class=HTMLResponse)
async def almanach_sync(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach = _require_almanach(request, session, almanach_id)
    result = await run_in_threadpool(
        almanach_lib.sync_almanach, session, almanach, "manual"
    )
    return templates.TemplateResponse(
        request,
        "partials/status.html",
        {
            "status": result,
            "status_title_ok": "Almanach erstellt",
            "status_title_error": "Almanach nicht erstellt",
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
    almanach = _require_almanach(request, session, almanach_id)
    target = f"/almanach/{almanach_id}"

    filename, error = _store_cover(
        covers.almanach_stem(almanach.id), await _read_cover(cover)
    )
    if error:
        return _cover_redirect(target, error=error)

    almanach.cover_path = filename
    almanach.cover_applied_at = None
    session.add(almanach)
    session.commit()

    status = await run_in_threadpool(
        _push_cover_quietly,
        almanach.plex_user_id,
        almanach.target_playlist_name,
        filename,
    )
    if status == "uebertragen":
        almanach.cover_applied_at = db.utcnow()
        session.add(almanach)
        session.commit()
    return _cover_redirect(target, status=status)


@app.post("/almanach/{almanach_id}/cover/delete")
async def almanach_cover_delete(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach = _require_almanach(request, session, almanach_id)
    covers.remove(covers.almanach_stem(almanach.id))
    almanach.cover_path = None
    almanach.cover_applied_at = None
    session.add(almanach)
    session.commit()
    await run_in_threadpool(
        _clear_cover_quietly, almanach.plex_user_id, almanach.target_playlist_name
    )
    return _cover_redirect(f"/almanach/{almanach_id}", status="entfernt")


@app.get("/almanach/{almanach_id}/cover/image")
async def almanach_cover_image(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    almanach = _require_almanach(request, session, almanach_id)
    return _serve_cover(almanach.cover_path)


# --- Watch-Status zurücksetzen: erst zeigen, dann bestätigen --------------


@app.get("/almanach/{almanach_id}/reset", response_class=HTMLResponse)
async def almanach_reset_confirm(
    request: Request, almanach_id: int, session: Session = Depends(db.get_session)
):
    """Erste Stufe: zeigt genau, was zurückgesetzt würde."""
    almanach = _require_almanach(request, session, almanach_id)
    plan = await run_in_threadpool(almanach_lib.plan_reset, session, almanach)
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
    """Zweite Stufe: führt den Reset aus und baut die Playlist neu."""
    almanach = _require_almanach(request, session, almanach_id)
    if confirm != "ja":
        plan = await run_in_threadpool(almanach_lib.plan_reset, session, almanach)
        return templates.TemplateResponse(
            request, "partials/almanach_reset.html", {"plan": plan, "almanach": almanach}
        )

    result = await run_in_threadpool(almanach_lib.reset_watch_state, session, almanach)
    sync = None
    if result.ok:
        sync = await run_in_threadpool(
            almanach_lib.sync_almanach, session, almanach, "reset"
        )
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

    users, _ = load_users()
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
