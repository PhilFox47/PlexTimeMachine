"""Übergangsclips zwischen den Tagen einer Zeitreise-Playlist.

Erzeugt kurze Videos im Look eines Senders: das Datum rollt vom vorherigen Tag
auf den nächsten, danach zeigt eine „UP NEXT"-Tafel das Tagesprogramm mit
Sendeplätzen. Gerendert wird mit Pillow (Einzelbilder) und FFmpeg (Kodierung) –
bewusst ohne Browser, damit das Image schlank bleibt.

Gestaltung: Fuchsbau Streaming – Schwarz, Orange, Weiß.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

FPS = 24

#: Mehr als das passt nicht lesbar auf eine Tafel.
MAX_ROWS = 10

SENDER = "FUCHSBAU"
SENDER_2 = "STREAMING"

# --- Farben ---------------------------------------------------------------
BLACK = (0, 0, 0)
INK = (9, 9, 9)
CARD = (26, 26, 26)
CARD_2 = (17, 17, 17)
CARD_LINE = (40, 40, 40)
ORANGE = (232, 130, 30)
ORANGE_HELL = (242, 160, 7)
ORANGE_TIEF = (206, 84, 12)
WEISS = (255, 255, 255)
GRAU = (154, 154, 154)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/TTF",
)


def _font_file(*kandidaten: str) -> str:
    """Erste vorhandene Schriftdatei – so überlebt der Renderer jedes Image."""
    for name in kandidaten:
        for ordner in _FONT_DIRS:
            pfad = Path(ordner) / name
            if pfad.exists():
                return str(pfad)
    raise RenderError(
        "Keine passende Schrift gefunden – im Image fehlen fonts-liberation "
        "und fonts-dejavu-core."
    )


class RenderError(RuntimeError):
    """Der Clip konnte nicht erzeugt werden."""


def _sans() -> str:
    return _font_file("LiberationSans-Regular.ttf", "DejaVuSans.ttf")


def _sans_bold() -> str:
    return _font_file("LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf")


def _sans_schmal() -> str:
    return _font_file(
        "LiberationSansNarrow-Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"
    )


# ---------------------------------------------------------------------------
# Inhalt
# ---------------------------------------------------------------------------


@dataclass
class ClipItem:
    """Ein Titel im Tagesprogramm."""

    kind: str  # "episode" | "movie"
    show: str
    title: str
    season: Optional[int] = None
    episode: Optional[int] = None
    year: Optional[int] = None
    slot: str = ""            # Sendeplatz "HH:MM"
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
        return self.items[:MAX_ROWS]

    @property
    def extra(self) -> int:
        return max(0, len(self.items) - MAX_ROWS)


# ---------------------------------------------------------------------------
# Zeichenwerkzeuge
# ---------------------------------------------------------------------------


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, max(8, int(size)))


def _ease(t: float) -> float:
    """Weiches Ein- und Ausschwingen."""
    t = min(1.0, max(0.0, t))
    return 3 * t * t - 2 * t * t * t


def _track_width(text: str, font: ImageFont.FreeTypeFont, tracking: float) -> float:
    """Breite eines Textes mit zusätzlicher Laufweite."""
    if not text:
        return 0.0
    return sum(font.getlength(z) for z in text) + tracking * (len(text) - 1)


def _track(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
    tracking: float,
    anchor: str = "ls",
) -> float:
    """Text mit Laufweite zeichnen (Pillow kann das nicht von sich aus).

    Gibt die gezeichnete Breite zurück. ``anchor`` versteht nur die hier
    gebrauchten Fälle: links/rechts und oben/mitte/Grundlinie.
    """
    breite = _track_width(text, font, tracking)
    x, y = xy
    if anchor[0] == "m":
        x -= breite / 2
    elif anchor[0] == "r":
        x -= breite
    for zeichen in text:
        draw.text((x, y), zeichen, font=font, fill=fill, anchor="l" + anchor[1])
        x += font.getlength(zeichen) + tracking
    return breite


def _shorten(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


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


def _gradient(size: tuple[int, int], links, rechts) -> Image.Image:
    """Waagerechter Verlauf – für Logo und Karten."""
    w, h = size
    band = Image.new("RGB", (max(1, w), 1))
    d = ImageDraw.Draw(band)
    for x in range(max(1, w)):
        f = x / max(1, w - 1)
        d.point((x, 0), fill=tuple(int(links[k] + (rechts[k] - links[k]) * f) for k in range(3)))
    return band.resize((max(1, w), max(1, h)))


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

#: Die Marke als Polygonzug im Einheitsquadrat – zwei Flächen, wie gefaltetes
#: Papier: ein heller Bogen von links oben zur Spitze und wieder hinauf, davor
#: ein dunkleres Dreieck oben rechts.
_MARKE_HELL = [(0.00, 0.00), (0.51, 0.335), (1.00, 0.656), (0.50, 1.00), (0.00, 0.656)]
_MARKE_DUNKEL = [(0.51, 0.335), (1.00, 0.00), (1.00, 0.656)]
_MARKE_VERHAELTNIS = 0.86   # Höhe zu Breite


def mark(width: int, hell=ORANGE_HELL, tief=ORANGE_TIEF) -> Image.Image:
    """Das Fuchsbau-Zeichen als RGBA-Bild."""
    w = max(4, int(width))
    h = max(4, int(w * _MARKE_VERHAELTNIS))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    maske = Image.new("L", (w, h), 0)
    ImageDraw.Draw(maske).polygon([(x * w, y * h) for x, y in _MARKE_HELL], fill=255)
    img.paste(_gradient((w, h), hell, tief), (0, 0), maske)

    dunkel = Image.new("L", (w, h), 0)
    ImageDraw.Draw(dunkel).polygon([(x * w, y * h) for x, y in _MARKE_DUNKEL], fill=255)
    img.paste(_gradient((w, h), tief, tuple(int(v * 0.82) for v in tief)), (0, 0), dunkel)

    # Falzschatten unter der Kante des dunklen Dreiecks
    schatten = Image.new("L", (w, h), 0)
    ImageDraw.Draw(schatten).polygon(
        [(0.51 * w, 0.335 * h), (0.60 * w, 0.395 * h), (0.51 * w, 0.42 * h)], fill=140
    )
    img.paste(
        Image.new("RGBA", (w, h), (60, 24, 4, 255)),
        (0, 0),
        Image.composite(schatten.filter(ImageFilter.GaussianBlur(max(1, w // 90))),
                        Image.new("L", (w, h), 0), maske),
    )
    return img


# ---------------------------------------------------------------------------
# Bühne
# ---------------------------------------------------------------------------


class Stage:
    """Maße, Schriften und der feste Bühnenhintergrund."""

    def __init__(self, height: int = 1080):
        self.h = int(height)
        self.w = int(round(self.h * 16 / 9))
        h = self.h

        self.sans = _sans()
        self.bold = _sans_bold()
        self.schmal = _sans_schmal()

        self.f_wort = _font(self.schmal, 0.052 * h)
        self.f_label = _font(self.schmal, 0.108 * h)
        self.f_fuss = _font(self.bold, 0.020 * h)
        self.f_wochentag = _font(self.schmal, 0.070 * h)
        self.f_datum = _font(self.bold, 0.150 * h)

        # Linke Spalte
        self.rand = 0.030 * self.w
        self.marke_breite = 0.070 * self.w
        self.marke_oben = 0.055 * h

        # Rechte Spalte
        self.linie_x = 0.356 * self.w
        self.karte_x0 = 0.375 * self.w
        self.karte_x1 = 0.972 * self.w
        self.liste_oben = 0.062 * h
        self.liste_unten = 0.885 * h
        self.fuss_y = 0.944 * h

        self.background = self._background()

    # -- Hintergrund -------------------------------------------------------

    def _background(self) -> Image.Image:
        """Schwarz mit einem sehr leichten Verlauf und der Ecke unten links."""
        img = Image.new("RGB", (self.w, self.h), BLACK)
        d = ImageDraw.Draw(img)
        for y in range(self.h):
            f = 1 - y / self.h
            d.line([(0, y), (self.w, y)], fill=(int(INK[0] * f), int(INK[1] * f), int(INK[2] * f)))

        # Die Marke noch einmal groß als Eckgrafik – angeschnitten, gedämpft.
        ecke = mark(int(0.40 * self.w), hell=(230, 146, 12), tief=(180, 68, 8))
        ecke = ecke.rotate(20, expand=True, resample=Image.BICUBIC)
        img.paste(ecke, (int(-0.115 * self.w), int(0.615 * self.h)), ecke)

        # Schwarzer Keil darüber: schneidet die Ecke diagonal an
        keil = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        ImageDraw.Draw(keil).polygon(
            [(0, 0), (self.w, 0), (self.w, self.h), (0.275 * self.w, self.h),
             (0.0, 0.620 * self.h)],
            fill=BLACK + (255,),
        )
        img.paste(keil, (0, 0), keil)
        return img

    # -- Kopf --------------------------------------------------------------

    def lockup(self, img: Image.Image) -> None:
        """Zeichen und Schriftzug oben links."""
        zeichen = mark(int(self.marke_breite))
        img.paste(zeichen, (int(self.rand), int(self.marke_oben)), zeichen)

        d = ImageDraw.Draw(img)
        x = self.rand + self.marke_breite + 0.014 * self.w
        mitte = self.marke_oben + zeichen.height / 2
        zeile = self.f_wort.size * 1.06
        _track(d, (x, mitte - zeile * 0.5), SENDER, self.f_wort, WEISS, 0.055 * self.f_wort.size, "lm")
        _track(d, (x, mitte + zeile * 0.5), SENDER_2, self.f_wort, WEISS, 0.055 * self.f_wort.size, "lm")

    def label(self, img: Image.Image, text: str, alpha: float = 1.0) -> None:
        """Die große Rubrik links – „UP NEXT" und Geschwister."""
        if alpha <= 0:
            return
        farbe = tuple(int(v * alpha) for v in WEISS)
        d = ImageDraw.Draw(img)
        y = 0.285 * self.h
        breite = _track(d, (self.rand, y), text, self.f_label, farbe,
                        0.10 * self.f_label.size, "lm")
        strich = tuple(int(v * alpha) for v in ORANGE)
        y2 = y + self.f_label.size * 0.72
        d.rectangle(
            [self.rand, y2, self.rand + min(breite, 0.075 * self.w), y2 + 0.0065 * self.h],
            fill=strich,
        )

    def footer(self, img: Image.Image, extra: int = 0) -> None:
        d = ImageDraw.Draw(img)
        x = self.karte_x0
        spur = 0.11 * self.f_fuss.size
        x += _track(d, (x, self.fuss_y), "ZEITEN IN 24H", self.f_fuss, ORANGE, spur, "lm")
        x += _track(d, (x + 0.012 * self.w, self.fuss_y), "•", self.f_fuss, GRAU, spur, "lm")
        text = "ALLE ANGABEN OHNE GEWÄHR"
        if extra:
            text += f"   •   +{extra} WEITERE"
        _track(d, (x + 0.022 * self.w, self.fuss_y), text, self.f_fuss, GRAU, spur, "lm")

    def base(self) -> Image.Image:
        img = self.background.copy()
        self.lockup(img)
        return img


# ---------------------------------------------------------------------------
# Programmtafel
# ---------------------------------------------------------------------------


def _poster(item: ClipItem, size: tuple[int, int]) -> Image.Image:
    """Das Poster aus Plex – oder ein ruhiger Platzhalter."""
    if item.poster:
        try:
            return _cover(Image.open(io.BytesIO(item.poster)).convert("RGB"), size)
        except Exception as exc:  # pragma: no cover - kaputte Bilddaten
            log.debug("Poster unbrauchbar (%s) – nutze Platzhalter", exc)

    w, h = size
    img = _gradient(size, (34, 34, 34), (22, 22, 22))
    d = ImageDraw.Draw(img)
    text = (item.show or item.title or "?").strip()
    for groesse in range(int(h * 0.20), 7, -1):
        f = _font(_sans_bold(), groesse)
        zeilen, aktuell = [], ""
        for wort in text.split():
            probe = f"{aktuell} {wort}".strip()
            if f.getlength(probe) <= w * 0.84:
                aktuell = probe
            else:
                if aktuell:
                    zeilen.append(aktuell)
                aktuell = wort
        if aktuell:
            zeilen.append(aktuell)
        if zeilen and all(f.getlength(z) <= w * 0.84 for z in zeilen) and \
                len(zeilen) * groesse * 1.3 <= h * 0.7:
            d.multiline_text((w / 2, h / 2), "\n".join(zeilen), font=f, fill=(150, 150, 150),
                             anchor="mm", align="center", spacing=int(groesse * 0.3))
            break
    return img


def _clapper(d: ImageDraw.ImageDraw, x: float, y: float, groesse: float, farbe) -> float:
    """Kleine Filmklappe vor dem Wort FILM. Gibt die belegte Breite zurück."""
    b = groesse
    h = b * 0.78
    strich = max(1, int(b * 0.09))
    oben = y - h / 2
    d.rounded_rectangle([x, oben + h * 0.34, x + b, oben + h], radius=max(1, int(b * 0.10)),
                        outline=farbe, width=strich)
    d.rounded_rectangle([x, oben, x + b, oben + h * 0.30], radius=max(1, int(b * 0.08)),
                        outline=farbe, width=strich)
    for k in (0.30, 0.62):
        d.line([(x + b * k, oben), (x + b * (k - 0.14), oben + h * 0.30)],
               fill=farbe, width=strich)
    return b


def _row(stage: Stage, item: ClipItem, size: tuple[int, int]) -> Image.Image:
    """Eine Programmzeile als fertiges Bild – wird je Clip nur einmal gebaut."""
    w, h = size
    radius = int(0.14 * h)
    karte = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    flaeche = _gradient((w, h), CARD, CARD_2).convert("RGBA")
    maske = Image.new("L", (w, h), 0)
    ImageDraw.Draw(maske).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    karte.paste(flaeche, (0, 0), maske)

    d = ImageDraw.Draw(karte)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, outline=CARD_LINE + (255,), width=1)

    # Poster links, bündig mit der Kartenhöhe
    pw = int(h * 2 / 3)
    poster = _poster(item, (pw, h)).convert("RGBA")
    ecken = Image.new("L", (pw, h), 0)
    ImageDraw.Draw(ecken).rounded_rectangle([0, 0, pw - 1, h - 1], radius=radius, fill=255)
    ImageDraw.Draw(ecken).rectangle([pw // 2, 0, pw - 1, h - 1], fill=255)
    karte.paste(poster, (0, 0), ecken)

    f_meta = _font(stage.bold, 0.185 * h)
    f_titel = _font(stage.sans, 0.265 * h)
    f_zeit = _font(stage.bold, 0.215 * h)
    spur = 0.06 * f_meta.size

    x = pw + 0.055 * w
    y_meta = 0.33 * h
    y_titel = 0.70 * h

    zeit = item.slot or ""
    zeit_breite = _track_width(zeit, f_zeit, 0.04 * f_zeit.size)
    rechts = w - 0.022 * w
    if zeit:
        _track(d, (rechts, y_meta), zeit, f_zeit, ORANGE, 0.04 * f_zeit.size, "rm")

    if item.kind == "episode" and item.season is not None and item.episode is not None:
        cursor = x
        cursor += _track(d, (cursor, y_meta), f"S{item.season:02d}", f_meta, ORANGE, spur, "lm")
        cursor += _track(d, (cursor + 0.014 * w, y_meta), "•", f_meta, (120, 120, 120), spur, "lm")
        _track(d, (cursor + 0.028 * w, y_meta), f"E{item.episode:02d}", f_meta, ORANGE, spur, "lm")
    elif item.kind == "episode":
        _track(d, (x, y_meta), _shorten(item.show, f_meta, w - x - zeit_breite - 0.06 * w),
               f_meta, ORANGE, spur, "lm")
    else:
        breite = _clapper(d, x, y_meta, f_meta.size * 1.05, ORANGE)
        _track(d, (x + breite + 0.016 * w, y_meta), "FILM", f_meta, ORANGE, spur, "lm")

    platz = rechts - x - (zeit_breite + 0.03 * w if zeit else 0)
    titel = item.title or item.show
    d.text((x, y_titel), _shorten(titel, f_titel, platz), font=f_titel, fill=WEISS, anchor="lm")
    return karte


class Board:
    """Die „UP NEXT"-Tafel: Zeilen einmal bauen, dann nur noch einblenden."""

    def __init__(self, stage: Stage, spec: ClipSpec):
        self.stage = stage
        self.spec = spec
        items = spec.shown or []
        n = max(1, len(items))

        spanne = stage.liste_unten - stage.liste_oben
        luecke = 0.013 * stage.h
        hoehe = min(0.152 * stage.h, (spanne - (n - 1) * luecke) / n)
        block = n * hoehe + (n - 1) * luecke
        oben = stage.liste_oben + (spanne - block) / 2

        self.breite = int(stage.karte_x1 - stage.karte_x0)
        self.hoehe = int(hoehe)
        self.positionen = [
            (int(stage.karte_x0), int(oben + k * (hoehe + luecke))) for k in range(n)
        ]
        self.zeilen = [_row(stage, item, (self.breite, self.hoehe)) for item in items]

        self.static = stage.base()
        stage.label(self.static, "UP NEXT")
        stage.footer(self.static, spec.extra)
        self.leer = stage.base()

    def frame(self, i: int, n: int) -> Image.Image:
        t = i / max(n - 1, 1)
        auftritt = _ease(t / 0.18) if t < 0.18 else 1.0
        img = (Image.blend(self.leer, self.static, auftritt)
               if auftritt < 1 else self.static.copy())

        d = ImageDraw.Draw(img)
        auftritte = [_ease((t - 0.06 - k * 0.045) / 0.22) for k in range(len(self.zeilen))]

        # Die Linie wächst genau so schnell, wie die Zeilen erscheinen – sie
        # hängt also nie ins Leere.
        oben = self.positionen[0][1]
        unten = oben
        for k, ein in enumerate(auftritte):
            if ein <= 0:
                break
            unten = self.positionen[k][1] + self.hoehe * min(1.0, ein)
        if unten > oben:
            d.rectangle(
                [self.stage.linie_x, oben,
                 self.stage.linie_x + max(2, self.stage.h // 480), unten],
                fill=ORANGE,
            )

        for k, (zeile, (x, y)) in enumerate(zip(self.zeilen, self.positionen)):
            ein = auftritte[k]
            if ein <= 0:
                continue
            versatz = int(0.035 * self.stage.w * (1 - ein))
            if ein >= 1:
                img.paste(zeile, (x, y), zeile)
            else:
                weich = zeile.copy()
                weich.putalpha(zeile.getchannel("A").point(lambda v: int(v * ein)))
                img.paste(weich, (x + versatz, y), weich)

            punkt = int(0.0085 * self.stage.h * min(1.0, ein * 1.4))
            mitte = y + self.hoehe / 2
            d.ellipse(
                [self.stage.linie_x - punkt + 1, mitte - punkt,
                 self.stage.linie_x + punkt + 1, mitte + punkt],
                fill=ORANGE,
            )
        return img


# ---------------------------------------------------------------------------
# Datumsrolle
# ---------------------------------------------------------------------------


def _odometer(stage: Stage, alt: str, neu: str, fortschritt: float) -> Image.Image:
    """Die Ziffern rollen spaltenweise um – jede Spalte sauber beschnitten."""
    f = stage.f_datum
    zellen: list[Image.Image] = []
    hoehe = int(f.size * 1.34)

    for spalte, (a, b) in enumerate(zip(alt.ljust(len(neu))[: len(neu)], neu)):
        breite = int(f.getlength(b) + f.size * 0.06)
        zelle = Image.new("RGBA", (breite, hoehe), (0, 0, 0, 0))
        d = ImageDraw.Draw(zelle)
        p = _ease(min(1.0, max(0.0, fortschritt * 1.45 - spalte * 0.05)))
        mitte = hoehe / 2
        if a == b or not a.strip():
            d.text((breite / 2, mitte), b, font=f, fill=ORANGE_HELL, anchor="mm")
        else:
            hub = hoehe
            d.text((breite / 2, mitte - hub * p), a, font=f,
                   fill=tuple(int(v * (1 - p * 0.5)) for v in ORANGE_HELL), anchor="mm")
            d.text((breite / 2, mitte + hub * (1 - p)), b, font=f,
                   fill=tuple(int(v * (0.5 + 0.5 * p)) for v in ORANGE_HELL), anchor="mm")
        zellen.append(zelle)

    gesamt = sum(z.width for z in zellen)
    band = Image.new("RGBA", (gesamt, hoehe), (0, 0, 0, 0))
    x = 0
    for zelle in zellen:
        band.paste(zelle, (x, 0), zelle)
        x += zelle.width
    return band


def _scene_date(stage: Stage, spec: ClipSpec, i: int, n: int) -> Image.Image:
    """Das Datum rollt vom Vortag auf den kommenden Sendetag."""
    t = i / max(n - 1, 1)
    roll = 0.0 if t < 0.28 else (1.0 if t > 0.64 else _ease((t - 0.28) / 0.36))

    img = stage.base()
    stage.label(img, "SENDETAG")
    d = ImageDraw.Draw(img)

    x = stage.karte_x0
    y_wochentag = 0.40 * stage.h
    # Nacheinander statt übereinander: der alte Tag ist weg, bevor der neue kommt.
    alt_alpha = max(0.0, 1 - roll * 2)
    neu_alpha = max(0.0, roll * 2 - 1)
    if alt_alpha > 0:
        _track(d, (x, y_wochentag), spec.prev_weekday.upper(), stage.f_wochentag,
               tuple(int(v * alt_alpha) for v in GRAU), 0.09 * stage.f_wochentag.size, "lm")
    if neu_alpha > 0:
        _track(d, (x, y_wochentag), spec.weekday.upper(), stage.f_wochentag,
               tuple(int(v * neu_alpha) for v in WEISS), 0.09 * stage.f_wochentag.size, "lm")

    d.rectangle([x, 0.455 * stage.h, x + 0.075 * stage.w, 0.455 * stage.h + 0.0065 * stage.h],
                fill=ORANGE)

    band = _odometer(stage, spec.prev_date, spec.date, roll)
    img.paste(band, (int(x), int(0.52 * stage.h)), band)

    if spec.items:
        vorschau = f"{len(spec.items)} TITEL IM PROGRAMM"
        _track(d, (x, 0.79 * stage.h), vorschau, stage.f_fuss, GRAU,
               0.11 * stage.f_fuss.size, "lm")
    return img


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------


def clip_duration(item_count: int) -> float:
    """Länge in Sekunden – die Tafel steht länger, wenn mehr zu lesen ist."""
    return 3.6 + min(7.0, 3.0 + 0.35 * min(item_count, MAX_ROWS)) + 1.0


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
    stage = Stage(height)
    board = Board(stage, spec)
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
            schreibe(_scene_date(stage, spec, i, frames_date))

        hold = int(min(7.0, 3.0 + 0.35 * len(spec.shown)) * FPS)
        letztes = None
        for i in range(hold):
            letztes = board.frame(i, hold)
            schreibe(letztes)

        schwarz = Image.new("RGB", (stage.w, stage.h), BLACK)
        outro = int(1.0 * FPS)
        for i in range(outro):
            schreibe(Image.blend(letztes or schwarz, schwarz, _ease(i / max(outro - 1, 1))))

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
