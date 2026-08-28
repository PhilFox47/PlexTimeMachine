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
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

FPS = 24

#: So viele Zeilen stehen gleichzeitig auf der Tafel – der Rest scrollt nach.
#: Wenige, dafür große Karten: die Poster sollen wirken.
VISIBLE_ROWS = 3

#: Irgendwo ist Schluss: darüber hinaus wird in der Fußzeile zusammengefasst.
MAX_ROWS = 24

#: Länge der einzelnen Abschnitte in Sekunden.
ROLLOVER_SECONDS = 3.6
FADE_SECONDS = 0.4
OUTRO_SECONDS = 1.0

#: Der mitgelieferte Klang unter der Datumsrolle.
DEFAULT_SOUND = Path(__file__).resolve().parent / "assets" / "transition_chime.aac"

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

# --- Farben der Datumsrolle (Cockpit-Optik, wie in der ersten Fassung) -----
LED_BG = (11, 13, 16)
AMBER = (255, 176, 32)
AMBER_DIM = (138, 95, 18)
TEAL = (41, 215, 200)
RED = (255, 77, 67)
LED_TEXT = (232, 228, 218)
LED_DIM = (142, 146, 153)

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


def _mono() -> str:
    return _font_file("DejaVuSansMono.ttf", "LiberationMono-Regular.ttf")


def _mono_bold() -> str:
    return _font_file("DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf")


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


def _rounded_mask(size: tuple[int, int], radius: float, scale: int = 4) -> Image.Image:
    """Abgerundete Maske ohne Treppen.

    Pillow zeichnet Rundungen hart; deshalb wird vierfach vergrößert gezeichnet
    und dann verkleinert. Das kostet fast nichts und ist der Unterschied
    zwischen „selbst gebastelt" und „sauber".
    """
    w, h = size
    gross = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(gross).rounded_rectangle(
        [0, 0, w * scale - 1, h * scale - 1], radius=max(1, int(radius * scale)), fill=255
    )
    return gross.resize((w, h), Image.LANCZOS)


def _ring_mask(size: tuple[int, int], radius: float, width: float, scale: int = 4) -> Image.Image:
    """Dasselbe für eine Kontur – als Maske, damit auch sie glatt bleibt."""
    w, h = size
    gross = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(gross).rounded_rectangle(
        [0, 0, w * scale - 1, h * scale - 1],
        radius=max(1, int(radius * scale)),
        outline=255,
        width=max(1, int(width * scale)),
    )
    return gross.resize((w, h), Image.LANCZOS)


def _circle(bild: Image.Image, mitte: tuple[float, float], radius: float, farbe,
            scale: int = 4) -> None:
    """Runder Punkt mit glatter Kante."""
    r = max(1, int(radius))
    kante = Image.new("L", (r * 2 * scale, r * 2 * scale), 0)
    ImageDraw.Draw(kante).ellipse([0, 0, r * 2 * scale - 1, r * 2 * scale - 1], fill=255)
    kante = kante.resize((r * 2, r * 2), Image.LANCZOS)
    flaeche = Image.new("RGBA", (r * 2, r * 2), tuple(farbe) + (255,))
    bild.paste(flaeche, (int(mitte[0] - r), int(mitte[1] - r)), kante)


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
        self.liste_oben = 0.070 * h
        self.liste_unten = 0.930 * h

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

    def subline(self, img: Image.Image, text: str) -> None:
        """Kleine Zeile unter der Rubrik – hier steht der Sendetag."""
        if not text:
            return
        d = ImageDraw.Draw(img)
        _track(d, (self.rand, 0.395 * self.h), text.upper(), self.f_fuss, GRAU,
               0.14 * self.f_fuss.size, "lm")

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
    radius = 0.085 * h
    karte = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # Fläche mit weicher Kante
    maske = _rounded_mask((w, h), radius)
    karte.paste(_gradient((w, h), CARD, CARD_2).convert("RGBA"), (0, 0), maske)
    # Hauchfeine Kontur – gibt der Karte eine Kante, ohne zu umranden
    kontur = Image.new("RGBA", (w, h), CARD_LINE + (255,))
    karte.paste(kontur, (0, 0), _ring_mask((w, h), radius, max(1, h // 220)))

    # Poster: eingerückt statt randlos, mit eigener Rundung
    luft = int(0.085 * h)
    ph = h - 2 * luft
    pw = int(ph * 2 / 3)
    poster = _poster(item, (pw, ph)).convert("RGBA")
    poster_maske = _rounded_mask((pw, ph), radius * 0.75)
    karte.paste(poster, (luft, luft), poster_maske)
    karte.paste(Image.new("RGBA", (pw, ph), (255, 255, 255, 26)), (luft, luft),
                _ring_mask((pw, ph), radius * 0.75, max(1, h // 260)))

    d = ImageDraw.Draw(karte)
    f_meta = _font(stage.bold, 0.125 * h)
    f_titel = _font(stage.sans, 0.200 * h)
    f_zeit = _font(stage.bold, 0.160 * h)
    spur = 0.07 * f_meta.size

    x = luft + pw + int(0.14 * h)
    rechts = w - 0.055 * h
    y_meta = 0.375 * h
    y_titel = 0.645 * h

    zeit = item.slot or ""
    zeit_breite = _track_width(zeit, f_zeit, 0.04 * f_zeit.size)
    if zeit:
        _track(d, (rechts, y_meta + 0.015 * h), zeit, f_zeit, ORANGE, 0.04 * f_zeit.size, "rm")

    # Kopfzeile: Nummer bzw. FILM, dahinter gedämpft die Herkunft.
    # ``setze`` rückt zuerst um den Abstand vor und schiebt dann um die Breite
    # weiter – sonst wandert alles Folgende nach links.
    cursor = x

    def setze(abstand: float, text: str, farbe) -> None:
        nonlocal cursor
        cursor += abstand
        cursor += _track(d, (cursor, y_meta), text, f_meta, farbe, spur, "lm")

    if item.kind == "episode" and item.season is not None and item.episode is not None:
        setze(0, f"S{item.season:02d}", ORANGE)
        setze(0.07 * h, "•", (110, 110, 110))
        setze(0.07 * h, f"E{item.episode:02d}", ORANGE)
        beiwerk = item.show or ""
    elif item.kind == "episode":
        beiwerk = item.show or ""
    else:
        cursor += _clapper(d, cursor, y_meta, f_meta.size * 1.05, ORANGE)
        setze(0.09 * h, "FILM", ORANGE)
        beiwerk = str(item.year or "")

    if beiwerk:
        platz = rechts - cursor - 0.30 * h - (zeit_breite if zeit else 0)
        if platz > f_meta.size * 3:
            setze(0.11 * h, "·", (90, 90, 90))
            setze(0.11 * h, _shorten(beiwerk, f_meta, platz), GRAU)

    platz = rechts - x - (zeit_breite + 0.16 * h if zeit else 0)
    titel = item.title or item.show
    d.text((x, y_titel), _shorten(titel, f_titel, platz), font=f_titel, fill=WEISS, anchor="lm")
    return karte


def _overflow_row(stage: Stage, anzahl: int, size: tuple[int, int]) -> Image.Image:
    """Schlusszeile ohne Karte: „+6 weitere Titel"."""
    w, h = size
    zeile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(zeile)
    f = _font(stage.bold, 0.150 * h)
    _track(d, (0.10 * h, h / 2), f"+{anzahl} WEITERE", f, GRAU, 0.14 * f.size, "lm")
    return zeile


def board_duration(item_count: int) -> float:
    """Wie lange die Tafel steht.

    Bis zu fünf Zeilen stehen einfach da; alles darüber scrollt gemächlich
    durch, damit auch ein voller Tag lesbar bleibt.
    """
    sichtbar = min(item_count, VISIBLE_ROWS)
    verweilen = 2.0 + 0.45 * sichtbar
    ueberhang = max(0, min(item_count, MAX_ROWS) - VISIBLE_ROWS)
    return verweilen + (ueberhang * 0.85 + 1.2 if ueberhang else 0.0)


class Board:
    """Die „UP NEXT"-Tafel: Zeilen einmal bauen, dann einblenden und scrollen."""

    def __init__(self, stage: Stage, spec: ClipSpec):
        self.stage = stage
        self.spec = spec
        items = spec.shown or []
        n = max(1, len(items))

        self.hoehe = int(0.255 * stage.h)     # feste Zeilenhöhe – Zeilen scrollen
        schritt = self.hoehe + int(0.020 * stage.h)
        gesamt = (n - 1) * schritt + self.hoehe

        # Ausschnitt: Zeitachse plus Karten. Alles darin wird beschnitten,
        # angeschnittene Zeilen enden also sauber am Rand.
        self.punkt = int(0.0095 * stage.h)
        self.fenster_x = int(stage.linie_x - self.punkt - 2)
        self.fenster_y = int(stage.liste_oben)
        self.fenster_w = int(stage.karte_x1) - self.fenster_x
        self.fenster_h = int(stage.liste_unten - stage.liste_oben)

        self.max_scroll = max(0, gesamt - self.fenster_h)
        oben = 0 if self.max_scroll else int((self.fenster_h - gesamt) / 2)

        self.breite = int(stage.karte_x1 - stage.karte_x0)
        self.karte_x = int(stage.karte_x0) - self.fenster_x
        self.linie_x = int(stage.linie_x) - self.fenster_x
        self.positionen = [oben + k * schritt for k in range(n)]
        self.zeilen = [_row(stage, item, (self.breite, self.hoehe)) for item in items]
        if spec.extra:
            # Der Rest bekommt keine Karte, sondern eine ruhige Schlusszeile.
            self.positionen.append(oben + n * schritt)
            self.zeilen.append(
                _overflow_row(stage, spec.extra, (self.breite, int(self.hoehe * 0.45)))
            )
            gesamt = self.positionen[-1] + int(self.hoehe * 0.45)
            self.max_scroll = max(0, gesamt - self.fenster_h)

        self.static = stage.base()
        stage.label(self.static, "UP NEXT")
        stage.subline(self.static, f"{spec.weekday} · {spec.date}")
        self.leer = stage.base()
        # Weiche Kanten nur dort, wo die Liste wirklich weitergeht.
        self.saum_oben = self._saum(True) if self.max_scroll else None
        self.saum_unten = self._saum(False) if self.max_scroll else None
        self.punkt = int(0.011 * stage.h)

    def _saum(self, oben: bool) -> Image.Image:
        """Auslaufende Kante – sonst reißen durchscrollende Zeilen hart ab."""
        maske = Image.new("L", (self.fenster_w, self.fenster_h), 255)
        d = ImageDraw.Draw(maske)
        tiefe = max(4, int(self.fenster_h * 0.05))
        for k in range(tiefe):
            y = k if oben else self.fenster_h - 1 - k
            d.line([(0, y), (self.fenster_w, y)], fill=int(255 * k / tiefe))
        return maske

    # -- Ablauf ------------------------------------------------------------

    def scroll_at(self, sekunden: float, dauer: float) -> float:
        """Wie weit die Liste zu diesem Zeitpunkt nach oben gewandert ist."""
        if not self.max_scroll:
            return 0.0
        beginn = 2.0 + 0.45 * VISIBLE_ROWS       # erst lesen lassen
        ende = max(beginn + 0.5, dauer - 1.0)    # unten kurz stehen bleiben
        return self.max_scroll * _ease((sekunden - beginn) / (ende - beginn))

    def frame(self, i: int, n: int) -> Image.Image:
        dauer = max(n, 1) / FPS
        sekunden = i / FPS
        t = i / max(n - 1, 1)

        auftritt = _ease(sekunden / 0.45) if sekunden < 0.45 else 1.0
        img = (Image.blend(self.leer, self.static, auftritt)
               if auftritt < 1 else self.static.copy())

        schicht = Image.new("RGBA", (self.fenster_w, self.fenster_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(schicht)
        scroll = self.scroll_at(sekunden, dauer)

        auftritte = [_ease((sekunden - 0.25 - k * 0.16) / 0.5) for k in range(len(self.zeilen))]

        # Die Zeitachse wächst mit den Zeilen und wandert danach mit ihnen.
        oben = self.positionen[0] - scroll
        unten = oben
        for k, ein in enumerate(auftritte):
            if ein <= 0:
                break
            unten = self.positionen[k] - scroll + self.zeilen[k].height * min(1.0, ein)
        if unten > oben:
            d.rectangle(
                [self.linie_x, max(0, oben),
                 self.linie_x + max(2, self.stage.h // 480), min(self.fenster_h, unten)],
                fill=ORANGE,
            )

        for k, zeile in enumerate(self.zeilen):
            ein = auftritte[k]
            if ein <= 0:
                continue
            y = int(self.positionen[k] - scroll)
            if y > self.fenster_h or y + zeile.height < 0:
                continue
            versatz = int(0.035 * self.stage.w * (1 - ein))
            if ein >= 1:
                schicht.alpha_composite(zeile, (self.karte_x, y))
            else:
                weich = zeile.copy()
                weich.putalpha(zeile.getchannel("A").point(lambda v: int(v * ein)))
                schicht.alpha_composite(weich, (self.karte_x + versatz, y))

            if k < len(self.spec.shown):     # die Schlusszeile bekommt keinen Punkt
                r = self.punkt * min(1.0, ein * 1.4)
                _circle(schicht, (self.linie_x + 1, y + zeile.height / 2), r, ORANGE)

        if self.max_scroll:
            kanal = schicht.getchannel("A")
            if scroll > 0.5:                       # oben geht es weiter
                kanal = ImageChops.multiply(kanal, self.saum_oben)
            if scroll < self.max_scroll - 0.5:     # unten kommt noch etwas
                kanal = ImageChops.multiply(kanal, self.saum_unten)
            schicht.putalpha(kanal)
        img.paste(schicht, (self.fenster_x, self.fenster_y), schicht)
        return img


# ---------------------------------------------------------------------------
# Datumsrolle – bewusst in der ursprünglichen Cockpit-Optik
# ---------------------------------------------------------------------------


class LedCanvas:
    """Bernstein-LED auf Schwarz: die Bühne der Datumsrolle.

    Sie hat ihre eigene Handschrift und bleibt deshalb unverändert, während die
    Programmtafel im Senderlook gestaltet ist.
    """

    def __init__(self, height: int = 1080):
        self.h = int(height)
        self.w = int(round(self.h * 16 / 9))
        s = self.h / 1080
        self.s = s

        mono, mono_b = _mono(), _mono_bold()
        self.f_caption = _font(mono, 26 * s)
        self.f_label = _font(mono, 30 * s)
        self.f_digit = _font(mono_b, 150 * s)
        self.f_weekday = _font(mono_b, 74 * s)
        self.background = self._background()

    def _background(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), LED_BG)
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
        feld = Image.new("RGB", cut.size, color)
        target.paste(feld, box[:2], cut.filter(ImageFilter.GaussianBlur(blur)).point(
            lambda v: int(v * 0.85 * alpha / 255)))
        target.paste(feld, box[:2], cut.point(lambda v: int(v * alpha / 255)))


def _scene_rollover(c: LedCanvas, spec: ClipSpec, i: int, n: int) -> Image.Image:
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
    c.glow(img, (c.w // 2, int(360 * s)), "DESTINATION DAY", c.f_label, LED_DIM, 6)

    if roll < 1:
        c.glow(img, (c.w // 2, int(460 * s)), spec.prev_weekday, c.f_weekday, RED, 14,
               int(255 * (1 - roll)))
    if roll > 0:
        c.glow(img, (c.w // 2, int(460 * s)), spec.weekday, c.f_weekday, AMBER, 18,
               int(255 * roll))

    zelle = c.f_digit.getlength("0")
    start = c.w // 2 - (len(spec.date) * zelle) / 2
    hub = 130 * s
    for spalte, (alt_z, neu_z) in enumerate(zip(spec.prev_date.ljust(len(spec.date)), spec.date)):
        x = start + spalte * zelle + zelle / 2
        if alt_z == neu_z:
            c.glow(img, (x, int(620 * s)), neu_z, c.f_digit, AMBER, 22)
            continue
        p = _ease(min(1.0, max(0.0, roll * 1.5 - spalte * 0.06)))
        if p < 1:
            c.glow(img, (x, int(620 * s) - hub * p), alt_z, c.f_digit, AMBER, 22,
                   int(255 * (1 - p)))
        if p > 0:
            c.glow(img, (x, int(620 * s) + hub * (1 - p)), neu_z, c.f_digit, AMBER, 22,
                   int(255 * p))

    if 0.44 < t < 0.56:  # Flux-Moment
        staerke = 1 - abs(t - 0.50) / 0.06
        img = Image.blend(img, Image.new("RGB", (c.w, c.h), TEAL), 0.18 * staerke)
        d = ImageDraw.Draw(img, "RGBA")
        for k in range(6):
            y = int((300 + k * 90) * s + 30 * s * math.sin(t * 40 + k))
            d.line([(0, y), (c.w, y)], fill=TEAL + (int(70 * staerke),), width=2)
    return img


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------


def clip_duration(item_count: int) -> float:
    """Länge in Sekunden – die Tafel steht länger, wenn mehr zu lesen ist."""
    return ROLLOVER_SECONDS + FADE_SECONDS + board_duration(item_count) + OUTRO_SECONDS


def sound_file(sound: Optional[str]) -> Optional[Path]:
    """Welcher Klang unter den Clip gelegt wird.

    Leer heißt „der mitgelieferte", ``off`` heißt stumm, alles andere ist ein
    Pfad. Fehlt die Datei, wird der Clip trotzdem erzeugt – nur eben still.
    """
    if sound and sound.strip().lower() in {"off", "aus", "none", "-"}:
        return None
    pfad = Path(sound.strip()) if sound and sound.strip() else DEFAULT_SOUND
    if not pfad.exists():
        log.warning("Klangdatei %s fehlt – der Übergang bleibt stumm", pfad)
        return None
    return pfad


def render_clip(
    spec: ClipSpec,
    target: Path,
    height: int = 1080,
    ffmpeg: str = "ffmpeg",
    sound: Optional[str] = None,
) -> Path:
    """Einen Übergang als MP4 schreiben (H.264, mit Tonspur).

    Auch ohne Klangdatei bekommt der Clip eine stille Tonspur: manche
    Plex-Clients stolpern über Videos ganz ohne Audio.
    """
    stage = Stage(height)
    board = Board(stage, spec)
    leinwand = LedCanvas(height)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordner = Path(tempfile.mkdtemp(prefix="ptm-clip-"))
    nummer = 0

    try:
        def schreibe(bild: Image.Image) -> None:
            nonlocal nummer
            bild.save(ordner / f"{nummer:05d}.png")
            nummer += 1

        frames_roll = int(ROLLOVER_SECONDS * FPS)
        letzte_rolle = None
        for i in range(frames_roll):
            letzte_rolle = _scene_rollover(leinwand, spec, i, frames_roll)
            schreibe(letzte_rolle)

        frames_tafel = int(board_duration(len(spec.shown)) * FPS)
        # Weicher Übergang von der Rolle auf die Tafel – kein harter Schnitt.
        frames_fade = int(FADE_SECONDS * FPS)
        erste_tafel = board.frame(0, frames_tafel)
        for i in range(frames_fade):
            schreibe(Image.blend(letzte_rolle, erste_tafel, _ease((i + 1) / frames_fade)))

        letzte_tafel = erste_tafel
        for i in range(frames_tafel):
            letzte_tafel = board.frame(i, frames_tafel)
            schreibe(letzte_tafel)

        schwarz = Image.new("RGB", (stage.w, stage.h), BLACK)
        outro = int(OUTRO_SECONDS * FPS)
        for i in range(outro):
            schreibe(Image.blend(letzte_tafel, schwarz, _ease(i / max(outro - 1, 1))))

        klang = sound_file(sound)
        laenge = nummer / FPS
        befehl = [ffmpeg, "-y", "-loglevel", "error",
                  "-framerate", str(FPS), "-i", str(ordner / "%05d.png")]
        if klang is None:
            befehl += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        else:
            # apad hängt Stille an, damit auch ein kürzerer Klang bis zum Ende
            # reicht; die genaue Länge setzt -t, sonst liefe die Stille weiter.
            befehl += ["-i", str(klang), "-af", "apad"]
        befehl += [
            "-t", f"{laenge:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(target),
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
