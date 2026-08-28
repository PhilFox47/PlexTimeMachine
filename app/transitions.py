"""Übergangsclips zwischen den Tagen einer Zeitreise-Playlist.

Erzeugt kurze Videos: das Datum rollt vom vorherigen Tag auf den nächsten,
danach zeigt eine Übersicht alle Titel dieses Tages. Gerendert wird mit Pillow
(Einzelbilder) und FFmpeg (Kodierung) – bewusst ohne Browser, damit das Image
schlank bleibt.

Die Optik ist die eigene Cockpit-Sprache der Oberfläche: Bernstein-LED auf
Schwarz mit Scanlines. Bewusst ohne Logos, Schriftzüge oder Bildmaterial aus
Filmen.
"""

from __future__ import annotations

import io
import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

FPS = 24
BG = (11, 13, 16)
AMBER = (255, 176, 32)
AMBER_DIM = (138, 95, 18)
TEAL = (41, 215, 200)
RED = (255, 77, 67)
TEXT = (232, 228, 218)
DIM = (142, 146, 153)

#: Schriften des Systems – im Image liefert fonts-dejavu sie mit.
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

#: Mehr als das passt nicht lesbar auf ein Bild.
MAX_TILES = 10


class RenderError(RuntimeError):
    """Der Clip konnte nicht erzeugt werden."""


@dataclass
class ClipItem:
    """Ein Titel in der Tagesübersicht."""

    kind: str  # "episode" | "movie"
    show: str
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    year: Optional[int] = None
    poster: Optional[bytes] = field(default=None, repr=False)


@dataclass
class ClipSpec:
    """Alles, was ein Übergang zeigt."""

    prev_weekday: str
    prev_date: str
    weekday: str
    date: str
    items: list[ClipItem] = field(default_factory=list)

    @property
    def shown(self) -> list[ClipItem]:
        return self.items[:MAX_TILES]

    @property
    def extra(self) -> int:
        return max(0, len(self.items) - MAX_TILES)


# ---------------------------------------------------------------------------
# Zeichenwerkzeuge
# ---------------------------------------------------------------------------


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _ease(t: float) -> float:
    """Weiches Ein- und Ausschwingen."""
    return 3 * t * t - 2 * t * t * t


class Canvas:
    """Hält Maße, Schriften und den vorgerechneten Hintergrund."""

    def __init__(self, height: int = 1080):
        self.h = height
        self.w = int(height * 16 / 9)
        s = height / 1080  # alles skaliert mit der Höhe

        self.f_caption = _font(MONO, int(26 * s))
        self.f_label = _font(MONO, int(30 * s))
        self.f_digit = _font(MONO_B, int(150 * s))
        self.f_weekday = _font(MONO_B, int(74 * s))
        self.f_title = _font(SANS_B, int(62 * s))
        self.f_sub = _font(MONO, int(34 * s))
        self.f_ep = _font(SANS_B, int(44 * s))
        self.f_small = _font(MONO, int(28 * s))
        self.s = s
        self.background = self._background()

    def _background(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BG)
        d = ImageDraw.Draw(img)
        for y in range(self.h):
            f = y / self.h
            d.line(
                [(0, y), (self.w, y)],
                fill=(int(11 + 16 * (1 - f)), int(13 + 20 * (1 - f)), int(16 + 26 * (1 - f))),
            )
        for y in range(0, self.h, max(2, int(4 * self.s))):
            d.line([(0, y), (self.w, y)], fill=(0, 0, 0))
        vignette = Image.new("L", (self.w, self.h), 0)
        ImageDraw.Draw(vignette).ellipse(
            [-self.w // 3, -self.h // 3, self.w + self.w // 3, self.h + self.h // 3], fill=255
        )
        vignette = vignette.filter(ImageFilter.GaussianBlur(int(220 * self.s)))
        return Image.composite(img, Image.new("RGB", (self.w, self.h), (0, 0, 0)), vignette)

    def glow(self, target, xy, text, font, color, blur=18, alpha=255, anchor="mm") -> None:
        """Text mit Leuchten. Nur der Textbereich wird geweichzeichnet – sonst
        kostet jedes Einzelbild ein Vielfaches."""
        if alpha <= 0 or not text:
            return
        blur = max(2, int(blur * self.s))
        mask = Image.new("L", (self.w, self.h), 0)
        ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor)
        box = mask.getbbox()
        if box is None:
            return
        pad = blur * 3
        box = (
            max(0, box[0] - pad),
            max(0, box[1] - pad),
            min(self.w, box[2] + pad),
            min(self.h, box[3] + pad),
        )
        cut = mask.crop(box)
        field_ = Image.new("RGB", cut.size, color)
        target.paste(field_, box[:2], cut.filter(ImageFilter.GaussianBlur(blur)).point(
            lambda v: int(v * 0.85 * alpha / 255)))
        target.paste(field_, box[:2], cut.point(lambda v: int(v * alpha / 255)))


def _shorten(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _poster_image(item: ClipItem, size: tuple[int, int]) -> Image.Image:
    """Das Poster aus Plex – oder ein schlichter Platzhalter, wenn keines da ist."""
    w, h = size
    if item.poster:
        try:
            img = Image.open(io.BytesIO(item.poster)).convert("RGB")
            return _cover(img, size)
        except Exception as exc:  # pragma: no cover - kaputte Bilddaten
            log.debug("Poster unbrauchbar (%s) – nutze Platzhalter", exc)

    img = Image.new("RGB", size, (26, 30, 38))
    d = ImageDraw.Draw(img)
    for y in range(h):
        f = y / h
        d.line([(0, y), (w, y)], fill=(int(26 * (1 - 0.5 * f)), int(30 * (1 - 0.5 * f)),
                                       int(38 * (1 - 0.5 * f))))
    d.rectangle([0, 0, w - 1, h - 1], outline=AMBER_DIM, width=max(1, w // 120))
    beschriftung = item.show or item.title
    for groesse in range(int(w * 0.14), 10, -2):
        f = _font(SANS_B, groesse)
        zeilen, aktuell = [], ""
        for wort in beschriftung.split():
            probe = f"{aktuell} {wort}".strip()
            if f.getlength(probe) <= w - 30:
                aktuell = probe
            else:
                if aktuell:
                    zeilen.append(aktuell)
                aktuell = wort
        if aktuell:
            zeilen.append(aktuell)
        if zeilen and all(f.getlength(z) <= w - 30 for z in zeilen) and \
                len(zeilen) * groesse * 1.3 <= h * 0.6:
            d.multiline_text((w // 2, h // 2), "\n".join(zeilen), font=f, fill=TEXT,
                             anchor="mm", align="center", spacing=int(groesse * 0.3))
            break
    return img


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Bild formatfüllend zuschneiden (wie CSS object-fit: cover)."""
    ziel = size[0] / size[1]
    quelle = img.width / img.height
    if quelle > ziel:
        neu = int(img.height * ziel)
        img = img.crop(((img.width - neu) // 2, 0, (img.width + neu) // 2, img.height))
    else:
        neu = int(img.width / ziel)
        img = img.crop((0, (img.height - neu) // 2, img.width, (img.height + neu) // 2))
    return img.resize(size, Image.LANCZOS)


def grid_for(count: int) -> tuple[int, int]:
    """Spalten und Zeilen für 1–10 Titel.

    Bewusst eine Tabelle statt einer Formel: so ist jede Anzahl gestaltet und
    nicht zufällig hübsch.
    """
    return {1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (4, 1),
            5: (3, 2), 6: (3, 2), 7: (4, 2), 8: (4, 2),
            9: (5, 2), 10: (5, 2)}.get(max(1, min(count, MAX_TILES)), (5, 2))


# ---------------------------------------------------------------------------
# Szenen
# ---------------------------------------------------------------------------


def _scene_date(c: Canvas, spec: ClipSpec, i: int, n: int) -> Image.Image:
    """Das Datum rollt wie ein Zählwerk um."""
    img = c.background.copy()
    t = i / max(n - 1, 1)
    roll = 0.0 if t < 0.30 else (1.0 if t > 0.62 else _ease((t - 0.30) / 0.32))
    s = c.s

    c.glow(img, (c.w // 2, int(150 * s)), "PLEX TIME MACHINE", c.f_caption, AMBER_DIM, 8)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle(
        (c.w // 2 - int(700 * s), int(300 * s), c.w // 2 + int(700 * s), int(760 * s)),
        int(10 * s), fill=(7, 9, 12, 235), outline=AMBER_DIM + (170,), width=2,
    )
    c.glow(img, (c.w // 2, int(360 * s)), "DESTINATION DAY", c.f_label, DIM, 6)

    if roll < 1:
        c.glow(img, (c.w // 2, int(460 * s)), spec.prev_weekday, c.f_weekday, RED, 14,
               int(255 * (1 - roll)))
    if roll > 0:
        c.glow(img, (c.w // 2, int(460 * s)), spec.weekday, c.f_weekday, AMBER, 18,
               int(255 * roll))

    cell = c.f_digit.getlength("0")
    start = c.w // 2 - (len(spec.date) * cell) / 2
    hub = 130 * s
    for col, (old, new) in enumerate(zip(spec.prev_date.ljust(len(spec.date)), spec.date)):
        x = start + col * cell + cell / 2
        if old == new:
            c.glow(img, (x, int(620 * s)), new, c.f_digit, AMBER, 22)
            continue
        p = _ease(min(1.0, max(0.0, roll * 1.5 - col * 0.06)))
        if p < 1:
            c.glow(img, (x, int(620 * s) - hub * p), old, c.f_digit, AMBER, 22,
                   int(255 * (1 - p)))
        if p > 0:
            c.glow(img, (x, int(620 * s) + hub * (1 - p)), new, c.f_digit, AMBER, 22,
                   int(255 * p))

    if 0.44 < t < 0.56:  # Flux-Moment
        staerke = 1 - abs(t - 0.50) / 0.06
        img = Image.blend(img, Image.new("RGB", (c.w, c.h), TEAL), 0.18 * staerke)
        d = ImageDraw.Draw(img, "RGBA")
        for k in range(6):
            y = int((300 + k * 90) * s + 30 * s * math.sin(t * 40 + k))
            d.line([(0, y), (c.w, y)], fill=TEAL + (int(70 * staerke),), width=2)
    return img


def _scene_overview(c: Canvas, spec: ClipSpec, i: int, n: int) -> Image.Image:
    """Alle Titel des Tages auf einem Bild, Kacheln blenden versetzt ein."""
    img = c.background.copy()
    t = i / max(n - 1, 1)
    out = 1.0 if t < 0.88 else 1 - _ease((t - 0.88) / 0.12)
    s = c.s
    items = spec.shown
    cols, rows = grid_for(len(items))

    c.glow(img, (c.w // 2, int(108 * s)), f"{spec.weekday}   {spec.date}", c.f_sub, AMBER, 10,
           int(255 * out))
    head = "COMING UP" if len(items) == 1 else f"COMING UP  ·  {len(items)} TITLES"
    if spec.extra:
        head += f"  (+{spec.extra})"
    c.glow(img, (c.w // 2, int(158 * s)), head, c.f_small, DIM, 5, int(255 * out))

    margin_x, top, gap = int(120 * s), int(215 * s), int(40 * s)
    bottom = int((60 if rows == 1 else 90) * s)
    area_w, area_h = c.w - 2 * margin_x, c.h - top - bottom
    cell_w = (area_w - (cols - 1) * gap) / cols
    cell_h = (area_h - (rows - 1) * gap) / rows

    if len(items) == 1:
        poster_h = int(min(cell_h * 0.95, 660 * s))
    else:
        poster_h = int(min(cell_h - (150 if rows == 1 else 130) * s, cell_w * 1.5 * 0.92))
    poster_w = int(poster_h * 2 / 3)

    f_show = _font(SANS_B, max(16, min(int(46 * s), int(poster_w * 0.13))))
    f_num = _font(MONO, max(13, min(int(30 * s), int(poster_w * 0.085))))
    f_title = _font(SANS_B, max(14, min(int(38 * s), int(poster_w * 0.105))))

    for idx, item in enumerate(items):
        col, row = idx % cols, idx // cols
        enter = _ease(min(1.0, max(0.0, (t - idx * 0.035) / 0.20)))
        alpha = int(255 * enter * out)
        if alpha <= 0:
            continue
        lift = int(35 * s * (1 - enter))

        in_row = min(cols, len(items) - row * cols)
        row_w = in_row * cell_w + (in_row - 1) * gap
        x0 = int((c.w - row_w) / 2 + col * (cell_w + gap))
        y0 = int(top + row * (cell_h + gap)) + lift

        if len(items) == 1:  # große Bühne statt einsamer Kachel
            total = poster_w + int(80 * s) + int(720 * s)
            px = int((c.w - total) / 2)
            py = int(top + (area_h - poster_h) / 2) + lift
            img.paste(_poster_image(item, (poster_w, poster_h)).point(
                lambda v: int(v * alpha / 255)), (px, py))
            tx, mid = px + poster_w + int(80 * s), py + poster_h // 2
            if item.kind == "episode":
                c.glow(img, (tx, mid - int(110 * s)), item.show, c.f_title, TEXT, 10, alpha, "lm")
                c.glow(img, (tx, mid - int(10 * s)),
                       f"SEASON {item.season}  ·  EPISODE {item.episode}", c.f_sub, TEAL, 10,
                       alpha, "lm")
                c.glow(img, (tx, mid + int(90 * s)), item.title, c.f_ep, AMBER, 12, alpha, "lm")
            else:
                c.glow(img, (tx, mid - int(60 * s)), item.title, c.f_title, TEXT, 10, alpha, "lm")
                c.glow(img, (tx, mid + int(40 * s)), f"MOVIE  ·  {item.year}", c.f_sub, TEAL,
                       10, alpha, "lm")
            continue

        px = int(x0 + (cell_w - poster_w) / 2)
        img.paste(_poster_image(item, (poster_w, poster_h)).point(
            lambda v: int(v * alpha / 255)), (px, y0))

        mid_x = int(x0 + cell_w / 2)
        ty = y0 + poster_h + int(34 * s)
        if item.kind == "episode":
            c.glow(img, (mid_x, ty), _shorten(item.show, f_show, cell_w), f_show, TEXT, 7, alpha)
            c.glow(img, (mid_x, ty + f_show.size + int(14 * s)),
                   f"S{item.season:02d}E{item.episode:02d}", f_num, TEAL, 6, alpha)
            c.glow(img, (mid_x, ty + f_show.size + f_num.size + int(30 * s)),
                   _shorten(item.title, f_title, cell_w), f_title, AMBER, 8, alpha)
        else:
            c.glow(img, (mid_x, ty), _shorten(item.title, f_show, cell_w), f_show, TEXT, 7, alpha)
            c.glow(img, (mid_x, ty + f_show.size + int(14 * s)), f"MOVIE · {item.year}",
                   f_num, TEAL, 6, alpha)
    return img


def _scene_outro(c: Canvas, spec: ClipSpec, i: int, n: int) -> Image.Image:
    img = c.background.copy()
    alpha = int(255 * (1 - _ease(i / max(n - 1, 1))))
    c.glow(img, (c.w // 2, c.h // 2 - int(40 * c.s)), spec.weekday, c.f_weekday, AMBER, 18, alpha)
    c.glow(img, (c.w // 2, c.h // 2 + int(60 * c.s)), spec.date, c.f_digit, AMBER, 22, alpha)
    return img


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------


def clip_duration(item_count: int) -> float:
    """Länge in Sekunden – die Übersicht steht länger, wenn mehr zu lesen ist."""
    return 3.6 + min(7.0, 3.0 + 0.35 * min(item_count, MAX_TILES)) + 1.0


def render_clip(
    spec: ClipSpec,
    target: Path,
    height: int = 1080,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Einen Übergang als MP4 schreiben (H.264, mit stiller Tonspur).

    Die stille Tonspur ist Absicht: manche Plex-Clients stolpern über Videos
    ganz ohne Audio.
    """
    canvas = Canvas(height)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordner = Path(tempfile.mkdtemp(prefix="ptm-clip-"))
    nummer = 0

    try:
        def schreibe(bild: Image.Image) -> None:
            nonlocal nummer
            bild.save(ordner / f"{nummer:05d}.png")
            nummer += 1

        frames_date = int(3.6 * FPS)
        for i in range(frames_date):
            schreibe(_scene_date(canvas, spec, i, frames_date))
        hold = int(min(7.0, 3.0 + 0.35 * len(spec.shown)) * FPS)
        for i in range(hold):
            schreibe(_scene_overview(canvas, spec, i, hold))
        outro = int(1.0 * FPS)
        for i in range(outro):
            schreibe(_scene_outro(canvas, spec, i, outro))

        befehl = [
            ffmpeg, "-y", "-loglevel", "error",
            "-framerate", str(FPS), "-i", str(ordner / "%05d.png"),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", str(target),
        ]
        ergebnis = subprocess.run(befehl, capture_output=True, text=True)
        if ergebnis.returncode != 0 or not target.exists():
            raise RenderError(
                f"FFmpeg fehlgeschlagen ({ergebnis.returncode}): "
                f"{ergebnis.stderr.strip()[:400] or 'keine Ausgabe'}"
            )
        return target
    except FileNotFoundError as exc:
        raise RenderError(
            f"FFmpeg nicht gefunden ('{ffmpeg}'). Im Container muss das Paket "
            f"installiert sein, sonst lassen sich keine Übergänge erzeugen."
        ) from exc
    finally:
        shutil.rmtree(ordner, ignore_errors=True)


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    """Prüft einmalig, ob ein FFmpeg mit H.264 bereitsteht."""
    try:
        ergebnis = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return ergebnis.returncode == 0 and "libx264" in ergebnis.stdout
