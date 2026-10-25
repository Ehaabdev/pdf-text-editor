"""Fonts for edited text: find the real font, shape text with it, embed it.

Word and Chrome embed only the glyphs a document uses (a subset) and strip the
shaping rules Arabic needs. New text is therefore shaped with the full font from
the font library (folders set by PDFEDIT_FONT_DIRS), found by its exact PostScript
name. The embedded subset is reused when it already has every glyph needed, drawn
identically; otherwise the needed glyphs of the full font are embedded as a new font.
"""

import hashlib
import io
import json
import os
import random
import string
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import logging

import pikepdf
import uharfbuzz as hb
from fontTools import subset as ft_subset
from fontTools.ttLib import TTCollection, TTFont

from . import config

logging.getLogger("fontTools").setLevel(logging.ERROR)


# Font library ---------------------------------------------------------------------------


def default_font_dirs() -> list[Path]:
    dirs = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft"
        # FontCache holds Office's cloud fonts (Aptos, Word's default since 2023), once used.
        dirs += [Path(windir) / "Fonts", local / "Windows" / "Fonts", local / "FontCache"]
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    return dirs + [config.BASE_DIR / "fonts"]


def font_dirs() -> list[Path]:
    configured = os.environ.get("PDFEDIT_FONT_DIRS")
    if configured:
        return [Path(p) for p in configured.split(os.pathsep) if p]
    return default_font_dirs()


@dataclass(frozen=True)
class FontEntry:
    path: str
    index: int  # face index inside a .ttc collection
    ps_name: str
    family: str
    bold: bool
    italic: bool
    full_name: str = ""


def _names(tt: TTFont) -> tuple[str, str, bool, bool, str]:
    name = tt["name"]
    ps = str(name.getDebugName(6) or "")
    full = str(name.getDebugName(4) or "")
    family = str(name.getDebugName(16) or name.getDebugName(1) or "")
    sub = str(name.getDebugName(17) or name.getDebugName(2) or "").lower()
    bold = "bold" in sub or ("OS/2" in tt and tt["OS/2"].usWeightClass >= 600)
    italic = "italic" in sub or "oblique" in sub
    return ps, family, bold, italic, full


class FontLibrary:
    def __init__(self, dirs: list[Path] | None = None):
        # The bundled look-alikes are always among the installed fonts: a word drawn in
        # Carlito by an earlier edit is in Carlito, exactly (and so are LibreOffice's).
        bundled = Path(__file__).resolve().parent.parent / "lookalikes"
        self.dirs = (dirs if dirs is not None else font_dirs()) + [bundled]
        self.entries: list[FontEntry] = self._load()
        self.by_ps = {e.ps_name.lower(): e for e in self.entries}
        self.by_full = {e.full_name.lower(): e for e in self.entries if e.full_name}

    def _load(self) -> list[FontEntry]:
        files = []
        for d in self.dirs:
            if d.is_dir():
                files += [p for p in d.rglob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc")]
        stamp = hashlib.sha1(
            "\n".join(f"{p}|{p.stat().st_size}|{int(p.stat().st_mtime)}" for p in sorted(files)).encode()
        ).hexdigest()
        cache = config.CACHE_DIR / "font-index.json"
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("stamp") == stamp:
                return [FontEntry(**e) for e in saved["entries"]]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        entries = []
        for p in files:
            try:
                faces = TTCollection(str(p), lazy=True).fonts if p.suffix.lower() == ".ttc" else [TTFont(str(p), lazy=True)]
            except Exception:
                continue
            for i, tt in enumerate(faces):
                try:
                    ps, family, bold, italic, full = _names(tt)
                except Exception:
                    continue
                if ps:
                    entries.append(FontEntry(str(p), i, ps, family, bold, italic, full))
        try:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"stamp": stamp, "entries": [e.__dict__ for e in entries]}), encoding="utf-8")
        except OSError:
            pass
        return entries

    def find(self, pdf_font_name: str) -> FontEntry | None:
        """The installed font a PDF font name refers to: the exact PostScript or full
        name, else the same family when every style word in the name is understood
        ("Arial,Bold", "TimesNewRomanPS-BoldItalicMT"). A name with any other style
        word ("Semibold", "Light", "Condensed") never falls back: that would be a
        different font."""
        name = pdf_font_name.split("+")[-1]
        if self._hidden(name):
            return None
        hit = self.by_ps.get(name.lower()) or self.by_full.get(name.lower())
        if hit:
            return hit
        family, style = _split_style(name)
        if family is None:
            return None
        bold, italic = "bold" in style, "italic" in style or "oblique" in style
        for e in self.entries:
            if e.family.replace(" ", "").lower() == family and e.bold == bold and e.italic == italic:
                return e
        return None

    @staticmethod
    def _hidden(name: str) -> bool:
        """PDFEDIT_HIDE_FONTS: fonts to treat as not installed, to see what a server without
        them would do ("calibri*,traditionalarabic": * matches any ending)."""
        hidden = [h.strip().lower() for h in os.environ.get("PDFEDIT_HIDE_FONTS", "").split(",") if h.strip()]
        plain = name.lower()
        return any(plain == h or (h.endswith("*") and plain.startswith(h[:-1])) for h in hidden)

    def is_exact(self, pdf_font_name: str) -> bool:
        name = pdf_font_name.split("+")[-1].lower()
        return name in self.by_ps or name in self.by_full


_STYLE_WORDS = {"regular", "normal", "roman", "bold", "italic", "oblique"}


def _split_style(name: str) -> tuple[str | None, set[str]]:
    """("arial", {"bold"}) from "Arial,Bold", "Arial-BoldMT" or "Arial Bold";
    (None, ...) when a style word isn't one of regular/bold/italic/oblique."""
    import re

    family, style = name, ""
    for sep in (",", "-"):
        if sep in family:
            family, _, style = family.partition(sep)
            break
    words = family.split(" ")
    while len(words) > 1 and words[-1].lower() in _STYLE_WORDS:
        style = words.pop() + " " + style
    family = "".join(words)
    for suffix in ("PSMT", "MT", "PS"):
        if family.endswith(suffix) and len(family) > len(suffix) + 2:
            family = family[: -len(suffix)]
        if style.endswith(suffix):
            style = style[: -len(suffix)]
    tokens = {t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", style)}
    if tokens - _STYLE_WORDS:
        return None, tokens
    return family.lower(), tokens


_library: FontLibrary | None = None


def library() -> FontLibrary:
    global _library
    if _library is None:
        _library = FontLibrary()
    return _library


def family_name(pdf_font_name: str) -> str:
    """The family name Word and PowerPoint know a PDF font by: "TimesNewRomanPS-BoldMT"
    -> "Times New Roman". The installed font's own name when it's here; otherwise the
    PDF's name, its style part dropped and its words spaced, as the website shows it."""
    import re

    entry = library().find(pdf_font_name)
    if entry is not None and entry.family:
        return entry.family
    name = pdf_font_name.split("+")[-1]
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"(PSMT|PS|MT)$", "", name)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return name.strip() or pdf_font_name


# Full fonts -----------------------------------------------------------------------------


class FullFont:
    """An installed font file: shapes text and supplies glyphs to embed."""

    def __init__(self, entry: FontEntry):
        self.entry = entry
        data = Path(entry.path).read_bytes()
        self.blob = hb.Blob(data)
        self.face = hb.Face(self.blob, entry.index)
        self.hb_font = hb.Font(self.face)
        self.upem = self.face.upem
        self.tt = (
            TTCollection(io.BytesIO(data)).fonts[entry.index] if entry.path.lower().endswith(".ttc") else TTFont(io.BytesIO(data))
        )
        self.glyph_order = self.tt.getGlyphOrder()
        self.hmtx = self.tt["hmtx"].metrics
        self.cmap = self.tt.getBestCmap() or {}

    def advance(self, gid: int) -> float:
        """Advance width in 1/1000 em."""
        return self.hmtx[self.glyph_order[gid]][0] * 1000 / self.upem

    def shape(self, text: str, rtl: bool, latin_ligatures: bool, kerning: bool):
        """Glyphs in page order (left to right): (gid, cluster, advance, x_offset, y_offset),
        all in 1/1000 em. Cluster is the index in `text` of the glyph's first character."""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        buf.direction = "rtl" if rtl else "ltr"
        features = {"kern": kerning}
        if not rtl:
            features.update({"liga": latin_ligatures, "clig": latin_ligatures})
        hb.shape(self.hb_font, buf, features)
        scale = 1000 / self.upem
        # uharfbuzz's add_str gives clusters as character indices into `text`.
        return [
            (info.codepoint, info.cluster, pos.x_advance * scale, pos.x_offset * scale, pos.y_offset * scale)
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions)
        ]

    def char_of_gid(self) -> dict[int, str]:
        """glyph id -> the plain character the font maps to it (shaped forms normalized)."""
        if not hasattr(self, "_char_of_gid"):
            import unicodedata

            table: dict[int, str] = {}
            for cp, name in sorted(self.cmap.items()):
                gid = self.tt.getGlyphID(name)
                ch = unicodedata.normalize("NFKC", chr(cp)) if 0xFB00 <= cp <= 0xFEFF else chr(cp)
                if len(ch) == 1:
                    table.setdefault(gid, ch)
            self._char_of_gid = table
        return self._char_of_gid

    def glyph_signature(self, gid: int):
        return _glyph_signature(self.tt, gid)


@lru_cache(maxsize=32)
def load_full_font(entry: FontEntry) -> FullFont:
    return FullFont(entry)


def _glyph_signature(tt: TTFont, gid: int):
    """Something that is equal for two fonts exactly when the glyph is drawn the same."""
    try:
        name = tt.getGlyphOrder()[gid]
    except IndexError:
        return None
    if "glyf" in tt:
        glyf = tt["glyf"]
        g = glyf[name]
        if g.isComposite():
            return ("composite", tuple((tt.getGlyphID(c.glyphName), getattr(c, "x", 0), getattr(c, "y", 0)) for c in g.components))
        if g.numberOfContours == 0:
            return ("empty",)
        coords, ends, flags = g.getCoordinates(glyf)
        return ("simple", tuple(coords), tuple(ends))
    if "CFF " in tt:
        cff = tt["CFF "].cff
        cs = cff[cff.fontNames[0]].CharStrings[name]
        cs.decompile()
        return ("cff", tuple(str(x) for x in cs.program))
    return None


# Embedded fonts -----------------------------------------------------------------------------


class EmbeddedFont:
    """A font already in the PDF (often a subset), seen from the font program inside."""

    def __init__(self, font_dict):
        self.tt = None
        descriptor = None
        if str(font_dict.get("/Subtype")) == "/Type0":
            descriptor = font_dict.DescendantFonts[0].get("/FontDescriptor")
        else:
            descriptor = font_dict.get("/FontDescriptor")
        stream = descriptor.get("/FontFile2") if descriptor is not None else None
        if stream is not None:
            try:
                self.tt = TTFont(io.BytesIO(stream.read_bytes()))
            except Exception:
                self.tt = None
        try:
            self.cmap = (self.tt.getBestCmap() or {}) if self.tt is not None else {}
        except Exception:  # subsets often have no character table at all
            self.cmap = {}

    def has_same_glyph(self, gid: int, full: FullFont) -> bool:
        if self.tt is None:
            return False
        mine = _glyph_signature(self.tt, gid)
        if mine is None or mine == ("empty",):
            return False
        return mine == full.glyph_signature(gid)

    def has_drawn_glyph(self, gid: int) -> bool:
        if self.tt is None:
            return False
        sig = _glyph_signature(self.tt, gid)
        return sig is not None and sig != ("empty",)

    def gid_for(self, char: str) -> int | None:
        name = self.cmap.get(ord(char))
        return self.tt.getGlyphID(name) if name is not None else None


class EmbeddingNotAllowed(Exception):
    """The font's licence (OS/2 fsType) doesn't allow embedding it in a document."""


def _tag() -> str:
    return "".join(random.choice(string.ascii_uppercase) for _ in range(6))


def _to_unicode_cmap(mapping: dict[int, str]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
        "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
    ]
    items = sorted(mapping.items())
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for gid, text in chunk:
            lines.append(f"<{gid:04X}> <{text.encode('utf-16-be').hex().upper()}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


def embed_font(pdf: pikepdf.Pdf, full: FullFont, used: dict[int, str]) -> pikepdf.Dictionary:
    """A Type0 font with the given glyphs of the full font, glyph ids kept (CID = GID).
    `used` maps each glyph id to the text it stands for (for copy and search)."""
    options = ft_subset.Options()
    options.retain_gids = True
    options.layout_features = []
    options.notdef_outline = True
    options.name_IDs = ["*"]
    options.drop_tables += ["GSUB", "GPOS", "GDEF", "kern", "DSIG", "morx", "BASE", "JSTF", "MATH", "meta"]
    fs_type = getattr(full.tt["OS/2"], "fsType", 0) if "OS/2" in full.tt else 0
    if fs_type & 0x000F == 0x0002:
        raise EmbeddingNotAllowed(full.entry.ps_name)
    if fs_type & 0x0100:  # the font's licence forbids subsetting: embed it whole
        program = _font_bytes(full)
    else:
        tt = TTFont(io.BytesIO(_font_bytes(full)))
        subsetter = ft_subset.Subsetter(options)
        subsetter.populate(gids=sorted(set(used) | {0}))
        subsetter.subset(tt)
        out = io.BytesIO()
        tt.save(out)
        program = out.getvalue()

    head, hhea = full.tt["head"], full.tt["hhea"]
    scale = 1000 / full.upem
    os2 = full.tt["OS/2"] if "OS/2" in full.tt else None
    italic_angle = float(full.tt["post"].italicAngle) if "post" in full.tt else 0.0
    name = f"{_tag()}+{full.entry.ps_name}"
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name("/" + name),
        Flags=4 | (64 if full.entry.italic else 0),
        FontBBox=[round(head.xMin * scale), round(head.yMin * scale), round(head.xMax * scale), round(head.yMax * scale)],
        ItalicAngle=italic_angle,
        Ascent=round(hhea.ascent * scale),
        Descent=round(hhea.descent * scale),
        CapHeight=round((getattr(os2, "sCapHeight", 0) or hhea.ascent) * scale),
        StemV=80,
        FontFile2=pdf.make_stream(program),
    )
    widths = []
    for gid in sorted(used):
        widths += [gid, [round(full.advance(gid), 3)]]
    cid_font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name("/" + name),
        CIDSystemInfo=pikepdf.Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0),
        FontDescriptor=pdf.make_indirect(descriptor),
        DW=1000,
        W=widths,
        CIDToGIDMap=pikepdf.Name.Identity,
    )
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type0,
            BaseFont=pikepdf.Name("/" + name),
            Encoding=pikepdf.Name("/Identity-H"),
            DescendantFonts=[pdf.make_indirect(cid_font)],
            ToUnicode=pdf.make_stream(_to_unicode_cmap(used)),
        )
    )


def _font_bytes(full: FullFont) -> bytes:
    if full.entry.path.lower().endswith(".ttc"):
        out = io.BytesIO()
        full.tt.save(out)
        return out.getvalue()
    return Path(full.entry.path).read_bytes()


# Look-alike fonts --------------------------------------------------------------------------
#
# When a PDF's font isn't installed and its embedded copy can't draw the new letters, a
# free font that looks like it stands in, and the user is asked to check the letters it
# drew. The files ship with the code (lookalikes/, with their licences and
# MANIFEST.json), so a server has them whatever is installed. Most Latin look-alikes have
# the same letter widths as the fonts they replace (Carlito for Calibri, Selawik for Segoe
# UI, Wine Tahoma for Tahoma); Arabic ones were chosen by eye and don't. Arabic look-alikes
# must draw each letter as one glyph: fonts that split letters into a base and separate
# dots (Noto Naskh Arabic, for one) give glyphs whose labels can't be right.
#
# Every other font gets a stand-in of its kind (stand_in): a serif, sans-serif or
# fixed-width font for Latin letters, a Naskh or a plainer one for Arabic, sized to match.

LOOKALIKE_DIR = Path(__file__).resolve().parent.parent / "lookalikes"

# Font family (as _family_style names it) -> (Latin look-alike, Arabic look-alike).
_LOOKALIKES: dict[str, tuple[str | None, str | None]] = {
    "calibri": ("Carlito", "IBMPlexSansArabic"),
    "cambria": ("Caladea", None),
    "arial": ("LiberationSans", "MarkaziText"),  # Arial's Arabic letters are Simplified Arabic's
    "helvetica": ("LiberationSans", None),
    "timesnewroman": ("LiberationSerif", "MarkaziText"),  # the same Arabic letters as Arial's
    "times": ("LiberationSerif", None),
    "couriernew": ("LiberationMono", None),
    "courier": ("LiberationMono", None),
    "segoeui": ("Selawik", "IBMPlexSansArabic"),  # Microsoft's free font with Segoe UI's widths
    "tahoma": ("WineTahoma", "IBMPlexSansArabic"),  # Wine's Tahoma: the same Latin widths
    "garamond": ("EBGaramond", None),
    "traditionalarabic": (None, "Amiri"),  # classical Naskh with stacked letter pairs
    "sakkalmajalla": (None, "Amiri"),
    "arabictypesetting": (None, "Amiri"),
    "simplifiedarabic": (None, "MarkaziText"),  # simplified Naskh, no stacked pairs
}
# Look-alikes whose letters are as wide as the original's.
_SAME_WIDTHS = {"Carlito", "Caladea", "LiberationSans", "LiberationSerif", "LiberationMono", "Selawik", "WineTahoma"}
_NAMES = {
    "Carlito": "Carlito", "Caladea": "Caladea", "LiberationSans": "Liberation Sans",
    "LiberationSerif": "Liberation Serif", "LiberationMono": "Liberation Mono", "Amiri": "Amiri",
    "MarkaziText": "Markazi Text", "IBMPlexSansArabic": "IBM Plex Sans Arabic", "Selawik": "Selawik",
    "WineTahoma": "Wine Tahoma", "EBGaramond": "EB Garamond",
}
_SEMIBOLD = {"semibold", "demibold", "demi", "sb"}
_BOLD = {"bold", "black", "heavy", "extrabold", "ultrabold"} | _SEMIBOLD
_LIGHT = {"light", "thin", "extralight", "ultralight", "semilight"}


@dataclass(frozen=True)
class Lookalike:
    entry: FontEntry
    name: str  # as people know it: "Carlito"
    same_widths: bool  # every letter as wide as in the font it stands in for
    close: bool = True  # made to look like this very font, not just one of its kind


def _family_style(pdf_font_name: str) -> tuple[str, set[str]]:
    """("segoeui", {"semibold"}) from "SegoeUI-Semibold": the family and its style words,
    whatever they are."""
    import re

    name = pdf_font_name.split("+")[-1]
    family, style = name, ""
    for sep in (",", "-"):
        if sep in family:
            family, _, style = family.partition(sep)
            break
    words = family.split(" ")
    while len(words) > 1 and words[-1].lower() in _STYLE_WORDS | _BOLD | _LIGHT:
        style = words.pop() + " " + style
    family = "".join(words)
    for suffix in ("PSMT", "MT", "PS"):
        if family.endswith(suffix) and len(family) > len(suffix) + 2:
            family = family[: -len(suffix)]
        if style.endswith(suffix):
            style = style[: -len(suffix)]
    tokens = {t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", style)}
    return family.lower(), tokens


def _file_for(base: str, tokens: set[str]) -> Path | None:
    """The bundled file of a family in the weight and style the words ask for: the
    nearest weight there is; None for a slant there's no file of."""
    italic = bool(tokens & {"italic", "oblique"})
    if tokens & _SEMIBOLD and (LOOKALIKE_DIR / f"{base}-Semibold.ttf").exists() and not italic:
        name = "Semibold"
    elif tokens & _BOLD:
        name = "BoldItalic" if italic else "Bold"
    elif tokens & _LIGHT and (LOOKALIKE_DIR / f"{base}-Light.ttf").exists() and not italic:
        name = "Light"
    else:
        name = "Italic" if italic else "Regular"
    path = LOOKALIKE_DIR / f"{base}-{name}.ttf"
    return path if path.exists() else None


def lookalike(pdf_font_name: str, arabic: bool) -> Lookalike | None:
    """The bundled look-alike made for a PDF font, in the same weight and style, for
    Latin or Arabic letters; None if there is none."""
    family, tokens = _family_style(pdf_font_name)
    if family not in _LOOKALIKES:
        return None
    base = _LOOKALIKES[family][1 if arabic else 0]
    if base is None:
        return None
    path = _file_for(base, tokens)
    if path is None:
        return None  # no italic Arabic look-alike, for one
    # The same widths only in the very weight asked for (a light font drawn by a regular
    # file isn't as wide).
    weight = path.stem.split("-")[-1].replace("Italic", "") or "Regular"
    asked = "Semibold" if tokens & _SEMIBOLD else "Bold" if tokens & _BOLD else "Light" if tokens & _LIGHT else "Regular"
    same = base in _SAME_WIDTHS and not arabic and weight == asked
    return Lookalike(_bundled_entry(str(path)), _NAMES[base], same)


_SERIF_NAMES = ("times", "roman", "serif", "garamond", "georgia", "cambria", "book", "palatino", "baskerville",
                "bodoni", "caslon", "minion", "didot", "century", "constantia", "bookman", "goudy", "perpetua",
                "rockwell", "charter", "lora", "merriweather", "playfair", "spectral")
_MONO_NAMES = ("courier", "mono", "consolas", "code", "typewriter", "menlo", "inconsolata")
_NASKH_NAMES = ("naskh", "traditional", "majalla", "amiri", "scheherazade", "lotus", "badr", "mitra", "nazanin",
                "zar", "typesetting", "uthman", "quran")
_PLAIN_ARABIC_NAMES = ("kufi", "sans", "tahoma", "segoe", "dubai", "cairo", "tajawal", "almarai", "calibri",
                       "noto", "plex", "helvetica", "frutiger", "din", "droid", "vazir", "yekan", "gess")


def stand_in(pdf_font_name: str, arabic: bool, serif: bool | None = None, fixed: bool | None = None, generic: bool = False) -> Lookalike:
    """A free font to draw letters the PDF's own font can't: its look-alike when there
    is one, else one of its kind. For Latin letters a serif, sans-serif or fixed-width
    font, as the PDF says the font is (serif, fixed) or its name suggests; for Arabic a
    Naskh or a plainer one. In the same weight and slant as far as there's a file.
    `generic`: one of its kind even when it has a look-alike (that lacks a letter)."""
    found = None if generic else lookalike(pdf_font_name, arabic)
    if found is not None:
        return found
    family, tokens = _family_style(pdf_font_name)
    if arabic:
        if any(k in family for k in _NASKH_NAMES):
            base = "Amiri"
        elif any(k in family for k in _PLAIN_ARABIC_NAMES):
            base = "IBMPlexSansArabic"
        else:
            base = "MarkaziText"
        tokens = tokens - {"italic", "oblique"}  # no slanted Arabic stand-ins
    else:
        if fixed is None:
            fixed = any(k in family for k in _MONO_NAMES)
        if serif is None:
            serif = any(k in family for k in _SERIF_NAMES)
        base = "LiberationMono" if fixed else "LiberationSerif" if serif else "LiberationSans"
    path = _file_for(base, tokens) or _file_for(base, tokens - {"italic", "oblique"}) or LOOKALIKE_DIR / f"{base}-Regular.ttf"
    return Lookalike(_bundled_entry(str(path)), _NAMES[base], False, close=False)


def embeddable(full: "FullFont") -> bool:
    """May the font be put in a PDF? (Its licence, in OS/2 fsType.)"""
    fs_type = getattr(full.tt["OS/2"], "fsType", 0) if "OS/2" in full.tt else 0
    return fs_type & 0x000F != 0x0002


@lru_cache(maxsize=64)
def _bundled_entry(path: str) -> FontEntry:
    tt = TTFont(path, lazy=True)
    ps, family, bold, italic, full = _names(tt)
    return FontEntry(path, 0, ps, family, bold, italic, full)
