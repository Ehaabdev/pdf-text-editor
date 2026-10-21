"""Find the lines of text on a page as a reader sees them.

A PDF only says "draw this glyph here". This module rebuilds lines from those
glyphs and gives each line its text in typing (logical) order, its writing
direction, its style and its position.

Source: MuPDF's text trace, which lists every glyph in drawing order with its
position and glyph id. A glyph standing for several characters (like the Arabic
lam-alef ligature) is followed by entries with glyph id -1, already in typing
order. MuPDF's own line building is not used: it splits lines that Word drew in
pieces, and scrambles mixed Arabic/English order and lam-alef ligatures.

Steps:
  1. Glyphs: merge ligature parts, drop invisible text, normalize shaped forms.
     Arabic glyphs are checked against the font's own character table: Word
     writes wrong characters for kashida-justified lines (a stretch glyph labelled
     "ز", a space labelled "يم") while the glyphs themselves are right.
  2. Rows: group glyphs on the same baseline; split at big gaps or table borders.
     Small raised or lowered pieces (superscripts) and vowel marks join the line
     they sit on. Invisible spaces drawn on top of letters are dropped.
  3. Direction: from the script at both ends of the line; when they differ, from
     which edge of its column the line starts at; else from the letter count.
  4. Order: glyphs sit left to right on the page; runs are put back into typing
     order, and brackets drawn mirrored in right-to-left runs are swapped back.

Coordinates are PDF points on the page as displayed (crop box and rotation applied).
"""

import math
import os
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

import pymupdf

LRI, RLI, PDI = "⁦", "⁧", "⁩"
TATWEEL = "ـ"

# Brackets a right-to-left renderer draws mirrored.
_MIRROR = dict(zip("()[]{}<>«»‹›", ")(][}{><»«›‹"))

_RTL_STRONG = {"R", "AL"}
_NUMBER = {"EN", "AN"}
_NUMBER_GLUE = {"ES", "ET", "CS"}  # 1,700.00  15%  +966
_NEUTRAL = {"WS", "CS", "ES", "ET", "ON", "S"}
_PERCENT = "%٪‰"


@dataclass(frozen=True)
class Style:
    font: str
    size: float
    color: str  # "rrggbb"
    bold: bool
    italic: bool
    superscript: bool


@dataclass
class Glyph:
    text: str  # characters in typing order, including attached vowel marks
    gid: int
    bbox: tuple[float, float, float, float]  # unrotated page space
    origin: tuple[float, float]
    style: Style
    direction: tuple[float, float]  # text direction, unrotated page space
    seq: int  # drawing order
    synthetic: bool = False  # a space inferred from a gap, not drawn
    drawn_as: str | None = None  # for brackets: the shape actually drawn, when known
    verified: bool | None = None  # Arabic glyph checked against the font's own table
    corrected: bool = False  # the PDF's label was wrong and was replaced from the font
    suspect: bool = False  # the PDF's label is wrong and the right one is unknown

    @property
    def base(self) -> str:
        return self.text[:1]

    @property
    def is_mark(self) -> bool:
        return bool(self.text) and unicodedata.category(self.base) in ("Mn", "Me")

    @property
    def is_space(self) -> bool:
        return self.text.isspace()

    @property
    def bidi(self) -> str:
        return (unicodedata.bidirectional(self.base) if self.text else "") or "ON"


@dataclass
class Run:
    rtl: bool
    glyphs: list[int]  # indices into Line.glyphs, typing order


@dataclass
class Line:
    id: str
    glyphs: list[Glyph]  # in page order along the text direction (left to right)
    rtl: bool
    runs: list[Run]  # typing order
    bbox: tuple[float, float, float, float]  # displayed page space
    spans: list[tuple[str, Style]] = field(default_factory=list)  # typing order
    kashida: bool = False  # stretched with tatweel glyphs (removed from the text)
    uncertain: bool = False  # some characters could not be confirmed

    def __post_init__(self):
        self._run_of = {i: r for r in self.runs for i in r.glyphs}

    @property
    def order(self) -> list[int]:
        """Glyph indices in typing order."""
        return [i for run in self.runs for i in run.glyphs]

    def glyph_text(self, i: int) -> str:
        g = self.glyphs[i]
        # In a right-to-left run a bracket is drawn mirrored. Word labels it with the
        # typed character already; other programs label the drawn shape, which then
        # has to be swapped back. Without knowing the shape, trust the label.
        if self._run_of[i].rtl and g.base in _MIRROR and g.drawn_as == g.base:
            return _MIRROR[g.base] + g.text[1:]
        return g.text

    @property
    def text(self) -> str:
        return "".join(self.glyph_text(i) for i in self.order)

    @property
    def display(self) -> str:
        """The text with direction isolates around embedded runs, so a standard bidi
        renderer (a browser, an edit box) shows it in the same order as the page."""
        # Every embedded run is isolated: without it, even digits after an Arabic word
        # in an English line ("التاريخ: 21/10/2026") would move to the other side.
        parts = []
        for run in self.runs:
            t = "".join(self.glyph_text(i) for i in run.glyphs)
            if run.rtl != self.rtl:
                t = (RLI if run.rtl else LRI) + t + PDI
            parts.append(t)
        return "".join(parts)

    @property
    def style(self) -> Style:
        """The style of most of the line's visible characters."""
        weight: dict[Style, int] = {}
        for g in self.glyphs:
            if not g.is_space and not g.synthetic:
                weight[g.style] = weight.get(g.style, 0) + len(g.text)
        return max(weight, key=weight.get) if weight else self.glyphs[0].style


# 1. Glyphs ---------------------------------------------------------------------------


def _style(span: dict) -> Style:
    name = span["font"].split("+")[-1]
    lowered = name.lower()
    flags = span["flags"]
    color = tuple(span["color"])
    if len(color) == 1:  # gray
        color = color * 3
    elif len(color) == 4:  # CMYK
        c, m, y, k = color
        color = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    r, g, b = (max(0, min(255, round(v * 255))) for v in color[:3])
    return Style(
        font=name,
        size=round(span["size"], 2),
        color=f"{r:02x}{g:02x}{b:02x}",
        bold=bool(flags & 16) or any(w in lowered for w in ("bold", "black", "heavy", "semibold", "demi")),
        italic=bool(flags & 2) or "italic" in lowered or "oblique" in lowered,
        superscript=bool(flags & 1),
    )


def _normalize(ch: str) -> str:
    cp = ord(ch)
    # Arabic presentation forms (shaped letters) and Latin ligatures back to plain letters.
    if 0xFB00 <= cp <= 0xFB06 or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFE:
        return unicodedata.normalize("NFKC", ch)
    if cp == 0xFEFF:
        return ""
    if cp == 0xF0B7:  # bullet in Word's Symbol font
        return "•"
    return ch


# Characters looked up in a font's own table to learn what each glyph shows.
_CHECKED = (
    [0x20, 0x640]
    + list(range(0x21, 0x7F))
    + list(range(0x621, 0x64B))
    + list(range(0x660, 0x66A))
    + list(range(0x671, 0x6D4))
    + list(range(0xFB50, 0xFDFD))
    + list(range(0xFE70, 0xFEFD))
    + [0xAB, 0xBB, 0x2039, 0x203A]
)

# Ligature glyphs whose standard decomposition uses Persian letters.
_LIGATURE_TEXT = {0xFDFC: "ريال"}  # Arial draws "ريال" as one glyph, the Rial sign


def _is_arabic_letter(ch: str) -> bool:
    cp = ord(ch)
    return 0x621 <= cp <= 0x64A or 0x671 <= cp <= 0x6D3 or 0xFB50 <= cp <= 0xFDFC or 0xFE70 <= cp <= 0xFEFC


@lru_cache(maxsize=256)
def _glyph_table(font_bytes: bytes) -> dict[int, frozenset[str]]:
    """glyph id -> every character the font itself maps to that glyph. One glyph can
    serve several characters: a medial Arabic yeh and a medial Persian yeh look the same."""
    try:
        font = pymupdf.Font(fontbuffer=font_bytes)
    except Exception:
        return {}
    table: dict[int, set[str]] = {}
    for cp in _CHECKED:
        gid = font.has_glyph(cp)
        if gid:
            text = _LIGATURE_TEXT.get(cp) or (unicodedata.normalize("NFKC", chr(cp)) if cp >= 0xFB00 else chr(cp))
            table.setdefault(gid, set()).add(text)
    return {gid: frozenset(chars) for gid, chars in table.items()}


def _font_tables(page: pymupdf.Page) -> dict[str, dict[int, frozenset[str]]]:
    """Per font name on the page, the union of its subsets' glyph tables. Word embeds
    one font several times (e.g. for Latin and for Arabic) with the same glyph ids."""
    doc = page.parent
    tables: dict[str, dict[int, frozenset[str]]] = {}
    for font in page.get_fonts(full=True):
        xref, ext, name = font[0], font[1], font[3].split("+")[-1]
        if ext not in ("ttf", "otf", "cff", "cid"):
            continue
        try:
            buf = doc.extract_font(xref)[3]
        except Exception:
            continue
        if buf:
            merged = tables.setdefault(name, {})
            for gid, chars in _glyph_table(buf).items():
                merged[gid] = merged.get(gid, frozenset()) | chars
    return tables


def _preferred(chars: frozenset[str]) -> str:
    # Standard Arabic letters before Persian/Urdu ones, single letters before ligatures.
    return min(chars, key=lambda c: (any(0x671 <= ord(x) <= 0x6FF for x in c), len(c), c))


def _check_glyph(g: Glyph, table: dict[int, frozenset[str]], space_gids: set[int]) -> None:
    chars = table.get(g.gid)
    if g.base in _MIRROR:
        if chars and g.base in chars:
            g.drawn_as = g.base
        elif chars and _MIRROR[g.base] in chars:
            g.drawn_as = _MIRROR[g.base]
        return
    if g.text.isspace() and chars is None:
        # Word labels some pieces of shaped Arabic letters as spaces. A "space" that
        # is not the font's space glyph and has no width is one of those; a wider one
        # stays a space but is left unconfirmed (verified None).
        width = g.bbox[2] - g.bbox[0]
        if space_gids and g.gid not in space_gids and width < 0.5:
            g.text, g.suspect = "", True
        return
    if g.text.isspace():
        g.verified = True if " " in chars else g.verified
    if chars is None:
        if any(_is_arabic_letter(c) for c in g.text):
            g.verified = False
        return
    # Only Arabic letters, spaces and tatweel are corrected. Latin letters, digits
    # and punctuation keep the PDF's label (Word may draw "2" with an Arabic-Indic shape).
    if not any(c in (" ", TATWEEL) or any(_is_arabic_letter(x) for x in c) for c in chars):
        return
    marks = "".join(c for c in g.text if unicodedata.combining(c))
    letters = "".join(c for c in g.text if not unicodedata.combining(c))
    if letters not in chars:
        g.text = _preferred(chars) + marks
        g.corrected = True
    g.verified = True


def _read_glyphs(page: pymupdf.Page) -> list[Glyph]:
    tables = _font_tables(page)
    glyphs: list[Glyph] = []
    seq = 0
    for span in page.get_texttrace():
        if span["type"] == 3 or span.get("opacity", 1) == 0:  # invisible text
            continue
        style = _style(span)
        dx, dy = span["dir"]
        norm = math.hypot(dx, dy) or 1.0
        direction = (round(dx / norm, 3) + 0.0, round(dy / norm, 3) + 0.0)
        start = len(glyphs)
        for code, gid, origin, bbox in span["chars"]:
            ch = _normalize(chr(code))
            if gid == -1 and len(glyphs) > start:
                glyphs[-1].text += ch  # another character of the same glyph
                continue
            glyphs.append(Glyph(ch, gid, tuple(bbox), tuple(origin), style, direction, seq))
            seq += 1
        table = tables.get(style.font, {})
        space_gids = {gid for gid, chars in table.items() if " " in chars}
        for g in glyphs[start:]:
            _check_glyph(g, table, space_gids)
    return [g for g in glyphs if g.text or g.suspect]


# 2. Rows -----------------------------------------------------------------------------


def _frame(direction):
    """Project a point onto the text direction (u) and its normal (v)."""
    dx, dy = direction
    return lambda p: (p[0] * dx + p[1] * dy, -p[0] * dy + p[1] * dx)


def _extent(g: Glyph, proj) -> tuple[float, float]:
    x0, y0, x1, y1 = g.bbox
    us = [proj(p)[0] for p in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    return min(us), max(us)


def _box(glyphs: list[Glyph]):
    return (
        min(g.bbox[0] for g in glyphs),
        min(g.bbox[1] for g in glyphs),
        max(g.bbox[2] for g in glyphs),
        max(g.bbox[3] for g in glyphs),
    )


def _vertical_rules(page: pymupdf.Page) -> list[tuple[float, float, float]]:
    """Table borders and other thin vertical strokes as (x, y0, y1), unrotated space."""
    rules = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return rules
    derot = page.derotation_matrix
    for d in drawings:
        for item in d["items"]:
            if item[0] == "l":
                p, q = item[1] * derot, item[2] * derot
                if abs(p.x - q.x) < 1 and abs(p.y - q.y) > 4:
                    rules.append(((p.x + q.x) / 2, min(p.y, q.y), max(p.y, q.y)))
            elif item[0] == "re":
                r = pymupdf.Rect(item[1]) * derot
                r.normalize()
                if r.width < 2.5 and r.height > 4:
                    rules.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
    return rules


def _rows(glyphs: list[Glyph], rules) -> list[list[Glyph]]:
    by_dir: dict = {}
    for g in glyphs:
        by_dir.setdefault(g.direction, []).append(g)

    pieces = []
    for direction, items in by_dir.items():
        proj = _frame(direction)
        items.sort(key=lambda g: proj(g.origin)[1])
        rows: list[list[Glyph]] = []
        for g in items:
            if rows:
                prev = rows[-1][-1]
                tol = 0.2 * max(g.style.size, prev.style.size)
                if abs(proj(g.origin)[1] - proj(prev.origin)[1]) <= tol:
                    rows[-1].append(g)
                    continue
            rows.append([g])

        ink_rows = [
            (proj(r[0].origin)[1], [_extent(g, proj) for g in r if not (g.is_space or g.is_mark)]) for r in rows
        ]

        def bridged(v: float, size: float, a: float, b: float) -> bool:
            # A justified line can have word gaps wider than a letter. A gap between
            # columns is empty in the lines above and below too; a word gap is not.
            lo, hi = a + 0.25 * (b - a), b - 0.25 * (b - a)
            return any(
                0.3 * size < abs(rv - v) < 2.6 * size and any(x0 < hi and x1 > lo for x0, x1 in extents)
                for rv, extents in ink_rows
            )

        for row in rows:
            row.sort(key=lambda g: _extent(g, proj)[0])
            v = proj(row[0].origin)[1]
            # Gaps are measured between visible glyphs only: Word draws spaces (and
            # end-of-cell marks) that straddle table borders.
            piece, last = [], None
            for g in row:
                if g.is_space or g.is_mark:
                    piece.append(g)
                    continue
                start, end = _extent(g, proj)
                if last is not None:
                    right = max(_extent(h, proj)[1] for h in piece if not (h.is_space or h.is_mark))
                    gap = start - right
                    size = max(g.style.size, last.style.size)
                    split = False
                    if gap > 0.5 and direction == (1.0, 0.0):
                        y0, y1 = min(last.bbox[1], g.bbox[1]), max(last.bbox[3], g.bbox[3])
                        split = any(right < x < start and ry0 < y1 and ry1 > y0 for x, ry0, ry1 in rules)
                    if not split and gap > 1.0 * size:
                        split = not bridged(v, size, right, start)
                    if split:
                        pieces.append(piece)
                        piece = []
                piece.append(g)
                last = g
            pieces.append(piece)
    return [p for p in pieces if any(not g.is_space for g in p)]


def _merge_small_pieces(pieces: list[list[Glyph]]) -> list[list[Glyph]]:
    """Superscripts, subscripts and stray vowel marks join the line they sit on."""
    pieces = [p for p in pieces if p]
    changed = True
    while changed:
        changed = False
        for i, p in enumerate(pieces):
            only_marks = all(g.is_mark or g.is_space for g in p)
            p_size = max(g.style.size for g in p)
            proj = _frame(p[0].direction)
            pu0 = min(_extent(g, proj)[0] for g in p)
            pu1 = max(_extent(g, proj)[1] for g in p)
            pb = _box(p)
            for j, q in enumerate(pieces):
                if i == j or q[0].direction != p[0].direction or len(q) < len(p):
                    continue
                q_size = max(g.style.size for g in q)
                if not (only_marks or p_size < 0.85 * q_size):
                    continue
                qu0 = min(_extent(g, proj)[0] for g in q)
                qu1 = max(_extent(g, proj)[1] for g in q)
                qb = _box(q)
                gap = max(qu0 - pu1, pu0 - qu1)
                overlap = min(pb[3], qb[3]) - max(pb[1], qb[1])
                if gap < 0.4 * q_size and overlap > 0.3 * (pb[3] - pb[1]):
                    q.extend(p)
                    pieces.pop(i)
                    changed = True
                    break
            if changed:
                break
    return pieces


def _attach_marks(glyphs: list[Glyph], proj) -> list[Glyph]:
    """Put each vowel mark (a zero-width glyph drawn over a letter) into its letter."""
    letters = [g for g in glyphs if not g.is_mark and not g.is_space]
    if not letters:
        return glyphs
    for m in (g for g in glyphs if g.is_mark):
        # A mark has no width; its shape is painted just right of its origin, which
        # can sit at the edge of the neighbouring letter.
        u = proj(m.origin)[0] + 0.6
        over = [b for b in letters if _extent(b, proj)[0] - 0.1 <= u <= _extent(b, proj)[1] + 0.1]
        if over:  # several letters under it: the one drawn right after (Word), else nearest
            best = min(over, key=lambda b: (abs(b.seq - m.seq), b.seq < m.seq))
        else:
            best = min(letters, key=lambda b: min(abs(e - u) for e in _extent(b, proj)))
        best.text += m.text
    return [g for g in glyphs if not g.is_mark]


def _drop_hidden_spaces(glyphs: list[Glyph], proj) -> list[Glyph]:
    """Word sometimes draws a space glyph on top of a letter (often a word's last
    letter). It is invisible and, sorted by position, would split the word."""
    ink = [_extent(g, proj) for g in glyphs if not g.is_space]
    kept: list[tuple[float, float]] = []  # spaces kept so far: two drawn on top of each other count once
    out = []
    for g in glyphs:
        if g.is_space:
            a, b = _extent(g, proj)
            covered = max((min(b, y) - max(a, x) for x, y in ink + kept), default=0)
            if covered > 0.5 * max(b - a, 0.01):
                continue
            kept.append((a, b))
        out.append(g)
    return out


def _add_gap_spaces(glyphs: list[Glyph], proj) -> list[Glyph]:
    """Some PDFs place words apart without drawing a space; add one where a reader sees a gap."""
    out = [glyphs[0]]
    right = _extent(glyphs[0], proj)[1]
    for g in glyphs[1:]:
        start, end = _extent(g, proj)
        prev = out[-1]
        if start - right > 0.22 * max(g.style.size, prev.style.size) and not prev.is_space and not g.is_space:
            out.append(Glyph(" ", 0, prev.bbox, prev.origin, prev.style, prev.direction, prev.seq, synthetic=True))
        out.append(g)
        right = max(right, end)
    return out


# 3. Direction ------------------------------------------------------------------------


def _column_bounds(pieces: list[list[Glyph]], rules) -> dict[int, tuple[float, float, float, float]]:
    """For each horizontal piece: (left edge of its column or table cell, right edge,
    the piece's own left, right). Edges are the nearest table borders, else the
    page's text margins."""
    flat = [p for p in pieces if p and p[0].direction == (1.0, 0.0)]
    if not flat:
        return {}
    page_left = min(_box(p)[0] for p in flat)
    page_right = max(_box(p)[2] for p in flat)
    out = {}
    for p in flat:
        x0, y0, x1, y1 = _box(p)
        lefts = [x for x, a, b in rules if x <= x0 + 1 and a < y1 and b > y0]
        rights = [x for x, a, b in rules if x >= x1 - 1 and a < y1 and b > y0]
        out[id(p)] = (max(lefts) if lefts else page_left, min(rights) if rights else page_right, x0, x1)
    return out


def _is_rtl(glyphs: list[Glyph], bounds=None) -> bool:
    strong = [g for g in glyphs if g.bidi in _RTL_STRONG or g.bidi == "L"]
    rtl = sum(len(g.text) for g in strong if g.bidi in _RTL_STRONG)
    ltr = sum(len(g.text) for g in strong if g.bidi == "L")
    if not rtl:
        # Only Latin letters, but a bracket drawn mirrored: the continuation of an
        # Arabic paragraph, like "Console)" after "(Agent".
        return any(g.drawn_as is not None and g.drawn_as != g.base for g in glyphs)
    if not ltr:
        return True
    left_rtl = strong[0].bidi in _RTL_STRONG
    right_rtl = strong[-1].bidi in _RTL_STRONG
    if left_rtl == right_rtl:
        # "عبر WhatsApp Business وخدمات", "Mr. خالد (Khalid)": the script at both ends decides.
        return right_rtl
    if bounds:
        # "الموقع: www.example.com" starts at its column's right edge.
        left, right, x0, x1 = bounds
        from_left, from_right = x0 - left, right - x1
        if from_right + 3 < from_left:
            return True
        if from_left + 3 < from_right:
            return False
    return rtl > ltr


# 4. Typing order ---------------------------------------------------------------------


def _islands(kinds: list[str]) -> list[tuple[int, int]]:
    """Maximal [start, end) ranges made of 'in' items, which may contain 'glue'
    items only between two 'in' items."""
    out, i, n = [], 0, len(kinds)
    while i < n:
        if kinds[i] != "in":
            i += 1
            continue
        j = i + 1
        while j < n:
            k = j
            while k < n and kinds[k] == "glue":
                k += 1
            if k < n and kinds[k] == "in":
                j = k + 1
            else:
                break
        out.append((i, j))
        i = j
    return out


def _with_signs(glyphs: list[Glyph], ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Extend number ranges over a sign or unit touching them: +966, -10, 15%."""
    out = []
    for a, b in ranges:
        while a > 0 and glyphs[a - 1].bidi in ("ES", "ET") and (not out or a - 1 >= out[-1][1]):
            a -= 1
        while b < len(glyphs) and glyphs[b].bidi == "ET":
            b += 1
        out.append((a, b))
    return out


def _order(glyphs: list[Glyph], rtl: bool) -> list[Run]:
    """Split a line (glyphs in page order) into runs in typing order."""
    n = len(glyphs)

    def no_bracket(i):
        return glyphs[i].base not in _MIRROR

    if rtl:
        # Left-to-right islands: Latin words and numbers with the spaces and
        # punctuation between them: an email, "+966 55 123 4567", "Q-2026-0417".
        kinds = [
            "in" if g.bidi == "L" or g.bidi in _NUMBER else "glue" if g.bidi in _NEUTRAL and no_bracket(i) else "out"
            for i, g in enumerate(glyphs)
        ]
        runs, pos = [], 0
        for a, b in _with_signs(glyphs, _islands(kinds)):
            if pos < a:
                runs.append(Run(True, list(range(pos, a))[::-1]))
            island = list(range(a, b))
            # Arabic typesetting (and Word) draws "10%" as "%10": the sign on the left.
            if len(island) > 1 and glyphs[a].base in _PERCENT and glyphs[a + 1].bidi in _NUMBER:
                island = island[1:] + island[:1]
            runs.append(Run(False, island))
            pos = b
        if pos < n:
            runs.append(Run(True, list(range(pos, n))[::-1]))
        return [r for r in reversed(runs) if r.glyphs]  # the rightmost run is read first

    # Left-to-right line: Arabic words, with the spaces, punctuation and Arabic
    # digits between them, are right-to-left islands. Numbers inside keep their order.
    kinds = [
        "in" if g.bidi in _RTL_STRONG else "glue" if (g.bidi in _NEUTRAL or g.bidi in _NUMBER) and no_bracket(i) else "out"
        for i, g in enumerate(glyphs)
    ]
    runs, pos = [], 0
    for a, b in _islands(kinds):
        if pos < a:
            runs.append(Run(False, list(range(pos, a))))
        inner = list(range(a, b))
        sub = [glyphs[i] for i in inner]
        nums = _with_signs(sub, _islands(["in" if g.bidi in _NUMBER else "glue" if g.bidi in _NUMBER_GLUE else "out" for g in sub]))
        pieces, p = [], 0
        for x, y in nums:
            if p < x:
                pieces.append((False, inner[p:x]))
            pieces.append((True, inner[x:y]))
            p = y
        if p < len(inner):
            pieces.append((False, inner[p:]))
        ordered = []
        for is_number, piece in reversed(pieces):
            ordered.extend(piece if is_number else piece[::-1])
        runs.append(Run(True, ordered))
        pos = b
    if pos < n:
        runs.append(Run(False, list(range(pos, n))))
    return [r for r in runs if r.glyphs]


# Page --------------------------------------------------------------------------------


def _display_bbox(page: pymupdf.Page, glyphs: list[Glyph]) -> tuple[float, float, float, float]:
    ink = [g for g in glyphs if not g.is_space and not g.synthetic] or glyphs
    r = pymupdf.Rect(_box(ink)) * page.rotation_matrix
    r.normalize()
    return (round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2))


def _spans(line: Line) -> list[tuple[str, Style]]:
    """Consecutive characters with the same style, in typing order. Spaces take the
    style of the text before them (Word often draws them in another font)."""
    out: list[tuple[str, Style]] = []
    last = None
    for i in line.order:
        g = line.glyphs[i]
        style = last if (g.is_space or g.synthetic) and last else g.style
        text = line.glyph_text(i)
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + text, style)
        else:
            out.append((text, style))
        if not g.is_space and not g.synthetic:
            last = g.style
    return out


def _continue_paragraph_direction(lines: list[Line]) -> None:
    """A line without Arabic letters can still end an Arabic paragraph
    ("legal@example.com." wrapped onto its own line). It is right-to-left when the line
    just above it in the same column is right-to-left and both end at the same right edge."""
    for line in lines:
        if line.rtl or any(g.bidi in _RTL_STRONG for g in line.glyphs):
            continue
        x0, y0, x1, y1 = line.bbox
        height = y1 - y0
        above = [
            l for l in lines
            if l is not line and l.bbox[3] <= y0 + 0.3 * height and y0 - l.bbox[3] < 0.8 * height
            and min(l.bbox[2], x1) - max(l.bbox[0], x0) > 0
        ]
        if not above:
            continue
        prev = max(above, key=lambda l: l.bbox[3])
        if prev.rtl and abs(prev.bbox[2] - x1) < 2.5 and x0 - prev.bbox[0] > 2.5:
            line.rtl = True
            line.runs = _order(line.glyphs, True)
            line.__post_init__()
            line.spans = _spans(line)


def page_lines(page: pymupdf.Page) -> list[Line]:
    rules = _vertical_rules(page)
    pieces = _merge_small_pieces(_rows(_read_glyphs(page), rules))
    bounds = _column_bounds(pieces, rules)
    lines = []
    for piece in pieces:
        proj = _frame(piece[0].direction)
        where = bounds.get(id(piece))
        suspect = any(g.suspect for g in piece)
        row = _attach_marks([g for g in piece if g.text], proj)
        row.sort(key=lambda g: _extent(g, proj)[0])
        row = _drop_hidden_spaces(row, proj)
        if not row:
            continue
        # Tatweel glyphs stretch words for justification; they are not text. Gaps are
        # measured with them still in place so a stretched word does not split.
        row = _add_gap_spaces(row, proj)
        kashida = any(g.base == TATWEEL for g in row)
        row = [g for g in row if g.base != TATWEEL]
        while row and (row[0].is_space or row[0].synthetic):
            row.pop(0)
        while row and (row[-1].is_space or row[-1].synthetic):
            row.pop()
        if not row:
            continue
        rtl = _is_rtl(row, where)
        line = Line(id="", glyphs=row, rtl=rtl, runs=_order(row, rtl), bbox=_display_bbox(page, row))
        line.spans = _spans(line)
        line.kashida = kashida
        # The PDF's labels proved wrong in this line and some glyphs could not be
        # checked against the font, or a glyph's real character is unknown: the text
        # may still contain mistakes.
        unchecked = any(g.verified is False for g in row) or any(
            g.is_space and not g.synthetic and g.verified is None for g in row
        )
        line.uncertain = suspect or (any(g.corrected for g in row) and unchecked)
        lines.append(line)

    _continue_paragraph_direction(lines)

    # Reading order: top to bottom, then left to right (right to left for Arabic).
    lines.sort(key=lambda l: (round(l.bbox[1] / 3), -l.bbox[2] if l.rtl else l.bbox[0]))
    for n, line in enumerate(lines):
        line.id = f"{page.number}-{n}"
    return lines


def line_json(line: Line) -> dict:
    def span_json(text: str, st: Style) -> dict:
        return {
            "text": text, "font": st.font, "size": st.size, "color": st.color,
            "bold": st.bold, "italic": st.italic, "superscript": st.superscript,
        }

    s = line.style
    return {
        "id": line.id,
        "text": line.text,
        "display": line.display,
        "dir": "rtl" if line.rtl else "ltr",
        "bbox": list(line.bbox),
        "font": s.font,
        "size": s.size,
        "color": s.color,
        "bold": s.bold,
        "italic": s.italic,
        "spans": [span_json(t, st) for t, st in line.spans],
        "uncertain": line.uncertain,
    }


@lru_cache(maxsize=64)
def _cached(path: str, mtime_ns: int, page_no: int):
    with pymupdf.open(path) as doc:
        if not 0 <= page_no < doc.page_count:
            raise IndexError(page_no)
        page = doc[page_no]
        return round(page.rect.width, 3), round(page.rect.height, 3), tuple(page_lines(page))


def lines_for(path, page_no: int):
    """(width, height, lines) of one page of a stored PDF, cached until the file changes."""
    return _cached(str(path), os.stat(path).st_mtime_ns, page_no)
