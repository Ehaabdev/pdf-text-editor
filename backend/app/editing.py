"""Change the text of one line, keeping its font, size, color, baseline and alignment.

How an edit works:
  1. The page's instructions are replayed (pdfcontent) and must agree with MuPDF on
     every glyph; the line's glyphs are found among them.
  2. The old line and the new text are split into words. Words that did not change
     keep their exact original glyphs. Changed words are shaped with the real font.
  3. The words are laid out the way the line was aligned (left, right, centered or
     justified). Words that end up where they already were are not touched at all;
     the others are taken out of their instructions and drawn again in a new block.
  4. The result is checked independently before it is accepted: the line reads back
     as typed (our reader and PDFium), nothing outside the line changed by a single
     pixel (MuPDF and PDFium), and no other page changed.
"""

import difflib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np
import pikepdf
import pymupdf
import pypdfium2 as pdfium

from . import fonts as fontlib
from .pdfcontent import (
    DrawnGlyph,
    FontInfo,
    Paint,
    PlacedGlyph,
    PlacedRun,
    Replay,
    Unsupported,
    draw_block,
    matches_trace,
    replay,
)
from .textlayout import Line, made_by_word, page_lines

log = logging.getLogger("pdfedit.editing")

_BIDI_CONTROLS = dict.fromkeys(map(ord, "‎‏⁦⁧⁨⁩‪‫‬‭‮؜"))
_SPACE_RUN = re.compile(r"(\s+)")


class EditRejected(Exception):
    """The edit can't be made safely. The message is shown to the user."""


@dataclass
class EditResult:
    pdf: bytes
    warnings: list[str] = field(default_factory=list)
    text: str = ""


def clean_text(text: str) -> str:
    """The text as stored: direction controls removed, NFC."""
    return unicodedata.normalize("NFC", text.translate(_BIDI_CONTROLS))


def comparable(text: str) -> str:
    return re.sub(r"\s+", " ", clean_text(text)).strip()


# Words ----------------------------------------------------------------------------------


def _script(text: str) -> str:
    """R (Arabic letters), L (Latin letters), NUM (digits) or N (punctuation only)."""
    classes = {unicodedata.bidirectional(c) for c in text}
    if classes & {"R", "AL"}:
        return "R"
    if "L" in classes:
        return "L"
    if classes & {"EN", "AN"}:
        return "NUM"
    return "N"


@dataclass
class Word:
    text: str
    script: str
    # An unchanged word from the page:
    traces: list[int] = field(default_factory=list)  # its drawn glyphs
    u0: float = 0.0  # left and right edge on the page (PDF x)
    u1: float = 0.0
    visual: int = -1  # position among the old line's words, left to right
    sep: str = ""  # what follows the word in the text: "" (joined) or a space
    logical: int = -1  # position in the new text
    # Its look (from the old word it replaces, or a neighbour):
    source: "Word | None" = None
    # Filled in by layout:
    rtl: bool = False
    width: float = 0.0
    x: float = 0.0  # new left edge
    shaped: list = field(default_factory=list)  # new glyphs: (gid, cluster, advance, dx, dy)
    choice: "FontChoice | None" = None

    @property
    def is_new(self) -> bool:
        return not self.traces


_BRACKET = set("()[]{}<>«»‹›")


def _split_words(text: str) -> list[tuple[str, str]]:
    """(word, separator after it). Words also split where Arabic meets Latin letters,
    and a bracket at either end is its own word: in Arabic text Word draws it on the
    far side of the word it belongs to."""
    parts = _SPACE_RUN.split(text)
    out = []
    for i in range(0, len(parts), 2):
        word, sep = parts[i], parts[i + 1] if i + 1 < len(parts) else ""
        if not word:
            continue
        pieces = []
        while word and word[0] in _BRACKET and len(word) > 1:
            pieces.append(word[0])
            word = word[1:]
        tail = []
        while word and word[-1] in _BRACKET and len(word) > 1:
            tail.insert(0, word[-1])
            word = word[:-1]
        current = word[0]
        for ch in word[1:]:
            a, b = _script(current[-1]), _script(ch)
            if {a, b} == {"R", "L"}:
                pieces.append(current)
                current = ch
            else:
                current += ch
        pieces.append(current)
        pieces += tail
        for k, p in enumerate(pieces):
            out.append((p, sep if k == len(pieces) - 1 else ""))
    return out


def _old_words(line: Line, rp: Replay) -> list[Word]:
    words: list[Word] = []
    current: list[int] = []

    def add(indices):
        text = "".join(line.glyph_text(i) for i in indices)
        traces = [t for i in indices for t in line.glyphs[i].traces]
        extents = [_extent(rp.glyphs[t]) for t in traces]
        words.append(Word(text, _script(text), traces, min(e[0] for e in extents), max(e[1] for e in extents)))

    def flush():
        if not current:
            return
        # Brackets at either end are words of their own, as in _split_words.
        head = []
        while len(current) > 1 and line.glyph_text(current[0]) in _BRACKET:
            head.append(current.pop(0))
        tail = []
        while len(current) > 1 and line.glyph_text(current[-1]) in _BRACKET:
            tail.insert(0, current.pop())
        for i in head:
            add([i])
        add(current)
        for i in tail:
            add([i])
        words[-1].sep = " "  # a space follows the group; its parts are joined
        current.clear()

    for i in line.order:
        g = line.glyphs[i]
        if g.is_space or g.synthetic:
            flush()
        else:
            current.append(i)
    flush()
    # Glyphs the reader set aside (kashida stretches, spaces drawn on top of letters,
    # unlabelled pieces) belong to the word they sit in, and move with it.
    claimed = {t for w in words for t in w.traces}
    for t in line.members:
        if t in claimed:
            continue
        a, b = _extent(rp.glyphs[t])
        mid = (a + b) / 2
        for w in words:
            if w.u0 - 0.01 <= mid <= w.u1 + 0.01:
                w.traces.append(t)
                w.u0, w.u1 = min(w.u0, a), max(w.u1, b)
                break
    for v, w in enumerate(sorted(words, key=lambda w: w.u0)):
        w.visual = v
    return words


def _extent(g: DrawnGlyph) -> tuple[float, float]:
    width = g.advance / 1000 * g.paint.size * g.paint.Tz * g.paint.trm2[0]
    return g.origin[0], g.origin[0] + max(width, 0.0)


# Fonts ----------------------------------------------------------------------------------


@dataclass
class FontChoice:
    resource: str | None  # existing font resource name, or None for a font to embed
    full: "fontlib.FullFont | None"
    font_info: FontInfo | None
    codes: list[int]  # one code per shaped glyph
    note: str | None = None


def _base_name(font_dict) -> str:
    return str(font_dict.get("/BaseFont", "")).lstrip("/").split("+")[-1]


class Fonts:
    def __init__(self, pdf: pikepdf.Pdf, page_index: int, rp: Replay, word_made: bool):
        self.pdf, self.page_index, self.rp, self.word_made = pdf, page_index, rp, word_made
        self.embedded: dict[str, fontlib.EmbeddedFont] = {}
        self.warnings: list[str] = []

    def _resources_named(self, name: str, prefer: str) -> list[str]:
        names = [r for r, info in self.rp.fonts.items() if info is not None and _base_name(info.obj) == name]
        return sorted(names, key=lambda r: (r != prefer, not self.rp.fonts[r].two_byte))

    def _embedded(self, resource: str) -> fontlib.EmbeddedFont:
        if resource not in self.embedded:
            self.embedded[resource] = fontlib.EmbeddedFont(self.rp.fonts[resource].obj)
        return self.embedded[resource]

    def shape(self, word: Word, paint: Paint, font_name: str) -> None:
        """Shape a new word and choose the font copy that will draw it."""
        text = word.text
        entry = fontlib.library().find(font_name)
        full = fontlib.load_full_font(entry) if entry else None
        candidates = self._resources_named(font_name, paint.font)
        needs_shaping = word.script == "R" or any(unicodedata.combining(c) for c in text)

        if full is not None:
            shaped = full.shape(text, word.rtl, latin_ligatures=not self.word_made, kerning=not self.word_made)
            missing = [text[c] for gid, c, *_ in shaped if gid == 0]
            if missing:
                raise EditRejected(f"The font {font_name} has no character for “{missing[0]}”.")
            word.shaped = shaped
            gids = [g[0] for g in shaped]
            for resource in candidates:
                codes = self._codes_in_copy(resource, gids, shaped, text, full)
                if codes is not None:
                    word.choice = FontChoice(resource, full, self.rp.fonts[resource], codes)
                    return
            word.choice = FontChoice(None, full, None, gids)
            return

        if needs_shaping:
            raise EditRejected(
                f"The font {font_name} isn't installed on the server, so new Arabic text can't be shaped with it."
            )
        # No installed font: Latin text can still use the embedded copy if it has every letter.
        for resource in candidates:
            info, emb = self.rp.fonts[resource], self._embedded(resource)
            shaped, codes = [], []
            for k, ch in enumerate(text):
                gid = emb.gid_for(ch)
                code = gid if info.two_byte else _winansi(ch)
                if gid is None or code is None or code not in info.widths or not (emb.has_drawn_glyph(gid) or ch.isspace()):
                    break
                shaped.append((gid, k, info.width(code), 0.0, 0.0))
                codes.append(code)
            else:
                word.shaped = shaped
                word.choice = FontChoice(resource, None, info, codes)
                return
        raise EditRejected(f"The font {font_name} isn't installed on the server and the PDF doesn't contain all the letters typed.")

    def _codes_in_copy(self, resource, gids, shaped, text, full) -> list[int] | None:
        """Codes to draw the glyphs with this embedded copy, if it has all of them drawn
        identically to the installed font."""
        info, emb = self.rp.fonts[resource], self._embedded(resource)
        if emb.tt is None:
            return None
        texts = _glyph_texts(text, shaped)
        codes = []
        for (gid, cluster, *_), wanted in zip(shaped, texts):
            same = emb.has_same_glyph(gid, full) or (full.glyph_signature(gid) == ("empty",) and gid < len(emb.tt.getGlyphOrder()))
            if not same:
                return None
            if info.two_byte:
                code = gid
            else:
                ch = text[cluster]
                code = _winansi(ch)
                if code is None or emb.gid_for(ch) != gid:
                    return None
            if code not in info.widths:
                return None
            # Copy and search in other programs read the copy's own labels: they must be right.
            label = info.to_unicode.get(code)
            if label is None or unicodedata.normalize("NFKC", label) != unicodedata.normalize("NFKC", wanted):
                return None
            codes.append(code)
        return codes

    def resource_for(self, full: "fontlib.FullFont", used: dict[int, str], cache: dict) -> str:
        key = full.entry
        if key not in cache:
            font = fontlib.embed_font(self.pdf, full, used)
            cache[key] = str(pikepdf.Page(self.pdf.pages[self.page_index]).add_resource(font, pikepdf.Name.Font, prefix="FE"))
        return cache[key]


def _winansi(ch: str) -> int | None:
    try:
        b = ch.encode("cp1252")
    except UnicodeEncodeError:
        return None
    return b[0] if len(b) == 1 else None


def _glyph_texts(text: str, shaped) -> list[str]:
    """For copy and search: the text each new glyph stands for. A ligature gets all its
    letters; a mark drawn as its own glyph gets just the mark."""
    clusters = sorted({c for _, c, *_ in shaped})
    span = {c: text[c : (clusters[i + 1] if i + 1 < len(clusters) else len(text))] for i, c in enumerate(clusters)}
    out, used = [], set()
    for gid, c, *_ in shaped:
        piece = span[c]
        if c not in used:
            used.add(c)
            out.append(piece if len(piece) == 1 or not any(unicodedata.combining(x) for x in piece) else "".join(x for x in piece if not unicodedata.combining(x)) or piece)
        else:
            marks = "".join(x for x in piece if unicodedata.combining(x))
            out.append(marks[:1] or piece[:1])
    return out


# Layout ---------------------------------------------------------------------------------


def _visual_order(words: list[Word], rtl: bool) -> list[Word]:
    """Words from typing order to left-to-right page order, the way Word arranges them:
    in an Arabic line, Latin words and numbers keep their order; in a Latin line,
    Arabic words (with numbers between them) are reversed."""
    n = len(words)
    if rtl:
        kinds = ["in" if w.script in ("L", "NUM") else "glue" if w.script == "N" else "out" for w in words]
    else:
        kinds = ["in" if w.script == "R" else "glue" if w.script in ("N", "NUM") else "out" for w in words]
    islands, i = [], 0
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
        islands.append((i, j))
        i = j
    # Runs in typing order: (right-to-left?, word indices). Islands have the other direction.
    runs, pos = [], 0
    for a, b in islands:
        if pos < a:
            runs.append((rtl, list(range(pos, a))))
        runs.append((not rtl, list(range(a, b))))
        pos = b
    if pos < n:
        runs.append((rtl, list(range(pos, n))))
    out: list[Word] = []
    for run_rtl, idx in (reversed(runs) if rtl else runs):
        for k in (idx[::-1] if run_rtl else idx):
            # Numbers read left to right even inside a right-to-left run.
            words[k].rtl = run_rtl and words[k].script != "NUM"
            out.append(words[k])
    return out


def _bracket_insides(words: list[Word], visual: list[Word]) -> set[tuple[int, int]]:
    """Pairs of visual positions (k, k+1) where a bracket touches the text it encloses.
    In an Arabic line Word draws "(Support Team)" with the brackets mirrored at the far
    ends, so the bracket typed first sits against the last word: the inside of a pair
    is found from where its partner bracket is, not from typing order."""
    opening, closing = set("([{<«‹"), set(")]}>»›")
    where = {id(w): k for k, w in enumerate(visual)}
    stack, out = [], set()
    for w in words:
        if w.text in opening:
            stack.append(w)
        elif w.text in closing and stack:
            o = stack.pop()
            a, b = where[id(o)], where[id(w)]
            lo, hi = min(a, b), max(a, b)
            # The text inside touches the bracket only when no space was typed there.
            if words[o.logical].sep == "" and hi - lo > 1:
                out.add((lo, lo + 1))
            if w.logical > 0 and words[w.logical - 1].sep == "" and hi - lo > 1:
                out.add((hi - 1, hi))
    return out


def _alignment(line: Line, old: list[Word], column: tuple[float, float], space: float) -> str:
    u0, u1 = old[0].u0, old[-1].u1
    left, right = column
    gaps = [b.u0 - a.u1 for a, b in zip(old, old[1:]) if b.u0 - a.u1 > 0.3 * space]
    touches_left, touches_right = u0 - left < 2.5, right - u1 < 2.5
    # A line reaching both edges of its column is justified, however little it was stretched.
    if line.kashida or (touches_left and touches_right and gaps):
        return "justify"
    if not touches_left and not touches_right and abs((u0 - left) - (right - u1)) < 3:
        return "center"
    return "right" if line.rtl else "left"


# The edit -----------------------------------------------------------------------------------


def replace_line(src: bytes, page_no: int, line_id: str, old_text: str, new_text: str, redraw_all: bool = False) -> EditResult:
    """redraw_all (for tests): draw every word anew, even unchanged ones, to compare
    the layout and shaping with the original."""
    new_text = clean_text(new_text)
    mu = pymupdf.open("pdf", src)
    if not 0 <= page_no < mu.page_count:
        raise EditRejected("That page doesn't exist.")
    page = mu[page_no]
    line = next((l for l in page_lines(page) if l.id == line_id), None)
    if line is None or comparable(line.text) != comparable(old_text):
        raise EditRejected("This line changed since you selected it. Select it again.")
    if comparable(new_text) == comparable(line.text) and not redraw_all:
        return EditResult(src, [], line.text)
    if "�" in new_text:
        raise EditRejected("Some letters on this line couldn't be read from the PDF (shown as �). Retype them before saving.")

    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported as e:
        raise EditRejected(str(e))
    if not matches_trace(rp, page):
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    line_paints = [rp.glyphs[t].paint for t in line.members]
    if any(abs(p.trm2[1]) > 1e-6 or abs(p.trm2[2]) > 1e-6 or p.trm2[0] <= 0 or p.trm2[3] <= 0 for p in line_paints):
        raise EditRejected("Rotated or slanted text can't be edited yet.")

    word_made = made_by_word(mu)
    old = _old_words(line, rp)
    if not old:
        raise EditRejected("This line has no text to change.")
    old_visual = sorted(old, key=lambda w: w.u0)
    to_pdf = ~page.transformation_matrix
    if line.column:
        a, b = pymupdf.Point(line.column[0], 0) * to_pdf, pymupdf.Point(line.column[1], 0) * to_pdf
        column = (min(a.x, b.x), max(a.x, b.x))
    else:
        column = (old_visual[0].u0, old_visual[-1].u1)
    main = rp.glyphs[old_visual[0].traces[0]]
    space = _space_width(rp, line)

    # Words: unchanged ones are reused, the rest are new.
    new_pairs = _split_words(new_text)
    old_texts = [w.text for w in old]
    new_texts = [w for w, _ in new_pairs]
    words: list[Word] = []
    opcodes = difflib.SequenceMatcher(None, old_texts, new_texts, autojunk=False).get_opcodes()
    tangled = any(b.u0 < a.u1 - 0.5 for a, b in zip(old_visual, old_visual[1:]))
    if redraw_all or tangled or line.kashida:
        opcodes = [("replace", 0, len(old_texts), 0, len(new_texts))]
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                old[i1 + k].sep = new_pairs[j1 + k][1]
                words.append(old[i1 + k])
            continue
        for j in range(j1, j2):
            source = old[min(i1 + (j - j1), i2 - 1)] if i2 > i1 else (old[i1 - 1] if i1 > 0 else old[0])
            words.append(Word(new_texts[j], _script(new_texts[j]), source=source, sep=new_pairs[j][1]))
    for k, w in enumerate(words):
        w.logical = k
    if not words:
        return _apply(src, pdf, mu, rp, page_no, line, old, [], column, word_made, [], new_text)

    visual = _visual_order(words, line.rtl)
    fonts = Fonts(pdf, page_no, rp, word_made)
    for w in visual:
        if w.is_new:
            src_glyph = rp.glyphs[w.source.traces[0]]
            style = _style_of(line, w.source)
            fonts.shape(w, src_glyph.paint, style)
            em = src_glyph.paint.size * src_glyph.paint.Tz * src_glyph.paint.trm2[0] / 1000
            w.width = sum(g[2] for g in w.shaped) * em
        else:
            w.width = w.u1 - w.u0

    # Gaps: as on the page between words that stay neighbours, else a typical gap.
    old_gaps = [b.u0 - a.u1 for a, b in zip(old_visual, old_visual[1:]) if b.u0 - a.u1 > 0.3 * space]
    typical = sorted(old_gaps)[len(old_gaps) // 2] if old_gaps else space
    inside = _bracket_insides(words, visual)
    gaps, joined = [], []
    for k, (a, b) in enumerate(zip(visual, visual[1:])):
        first = words[min(a.logical, b.logical)]
        is_joined = (abs(a.logical - b.logical) == 1 and first.sep == "") or (k, k + 1) in inside
        if not a.is_new and not b.is_new and b.visual == a.visual + 1:
            gaps.append(b.u0 - a.u1)
            is_joined = is_joined or b.u0 - a.u1 < 0.3 * space  # touching on the page
        elif is_joined:
            gaps.append(0.0)
        else:
            gaps.append(typical)
        joined.append(is_joined)

    mode = _alignment(line, old_visual, column, space)
    u0, u1 = old_visual[0].u0, old_visual[-1].u1
    natural = sum(w.width for w in visual) + sum(gaps)
    stretchable = [i for i, j in enumerate(joined) if not j]
    layout_facts = lambda: (
        f"mode={mode} column={[round(c, 1) for c in column]} line={round(u0, 1)}..{round(u1, 1)} "
        f"natural={round(natural, 1)} space={round(space, 2)} gaps={[round(g, 1) for g in gaps]} "
        f"words={[(w.text, round(w.width, 1), 'new' if w.is_new else 'old') for w in visual]}"
    )
    if mode == "justify" and stretchable:
        # Only the spaces between words stretch; joined pieces ("(" and its word) stay together.
        extra = ((u1 - u0) - natural) / len(stretchable)
        for i in stretchable:
            gaps[i] += extra
        if min(gaps[i] for i in stretchable) < 0.5 * space:
            log.info("too long (justified): %s", layout_facts())
            raise EditRejected("The new text is too long to fit on this line. Shorten it a little.")
        start = u0
    elif mode == "center":
        start = (u0 + u1) / 2 - natural / 2
    elif mode == "right":
        start = u1 - natural
    else:
        start = u0
    x = start
    for w, gap in zip(visual, gaps + [0.0]):
        w.x = x
        x += w.width + gap
    room = (min(column[0], u0) - 0.5, max(column[1], u1) + 0.5)
    if visual[0].x < room[0] or visual[-1].x + visual[-1].width > room[1]:
        log.info("too long: %s", layout_facts())
        raise EditRejected("The new text is too long to fit on this line. Shorten it a little.")

    warnings = list(fonts.warnings)
    if line.kashida:
        warnings.append("This line was stretched with Arabic kashida; it is now justified with wider spaces instead.")
    return _apply(src, pdf, mu, rp, page_no, line, old, visual, column, word_made, warnings, new_text, gaps=gaps, fonts=fonts, space=space)


def _style_of(line: Line, word: Word) -> str:
    """The font name of a word's main glyph, as the PDF names it."""
    for g in line.glyphs:
        if g.traces and g.traces[0] == word.traces[0]:
            return g.style.font
    return line.style.font


def _space_width(rp: Replay, line: Line) -> float:
    for g in line.glyphs:
        if g.is_space and not g.synthetic and g.traces:
            d = rp.glyphs[g.traces[0]]
            return d.advance / 1000 * d.paint.size * d.paint.Tz * d.paint.trm2[0]
    size = line.style.size
    return 0.25 * size


def _apply(src, pdf, mu, rp, page_no, line, old, visual, column, word_made, warnings, new_text, gaps=None, fonts=None, space=0.0):
    # Words that land exactly where they were stay untouched.
    untouched = {id(w) for w in visual if not w.is_new and abs(w.x - w.u0) < 1e-4}
    keep = {t for w in old if id(w) in untouched for t in w.traces}
    # Spaces between two untouched words stay as well.
    kept_spaces = set()
    for a, b in zip(visual, visual[1:]):
        if id(a) in untouched and id(b) in untouched and b.visual == a.visual + 1:
            kept_spaces |= {t for t in line.members if a.u1 - 0.01 <= rp.glyphs[t].origin[0] <= b.u0 + 0.01 and t not in keep}
    remove = set(line.members) - keep - kept_spaces
    if not remove:
        return EditResult(src, warnings, line.text)

    runs: list[PlacedRun] = []
    embed_cache: dict = {}
    new_fonts: dict = {}  # FullFont entry -> used gids -> text
    for w in visual:
        if id(w) in untouched:
            continue
        if not w.is_new:
            dx = w.x - w.u0
            for t in w.traces:
                d = rp.glyphs[t]
                runs.append(PlacedRun(d.paint.font, rp.fonts[d.paint.font], d.paint, [PlacedGlyph(d.code, (d.origin[0] + dx, d.origin[1]))]))
            continue
        choice = w.choice
        if choice.resource is None:
            used = new_fonts.setdefault(choice.full.entry, {})
            for (gid, *_), text in zip(w.shaped, _glyph_texts(w.text, w.shaped)):
                used.setdefault(gid, text)
    for entry, used in new_fonts.items():
        fonts.resource_for(fontlib.load_full_font(entry), used, embed_cache)

    for w in visual:
        if not w.is_new:
            continue
        src_glyph = rp.glyphs[w.source.traces[0]]
        paint = src_glyph.paint
        resource = w.choice.resource or embed_cache[w.choice.full.entry]
        info = w.choice.font_info or FontInfo(resource, pdf.pages[page_no].obj.Resources.Font[resource])
        draw_paint = Paint(resource, paint.size, paint.Tz, paint.Tr, paint.trm2, paint.fill, paint.stroke, paint.extgstates)
        em_x = paint.size * paint.Tz * paint.trm2[0] / 1000
        em_y = paint.size * paint.trm2[3] / 1000
        pen, placed = 0.0, []
        baseline = src_glyph.origin[1]
        for (gid, cluster, adv, dx, dy), code in zip(w.shaped, w.choice.codes):
            placed.append(PlacedGlyph(code, (w.x + (pen + dx) * em_x, baseline + dy * em_y)))
            pen += adv
        runs.append(PlacedRun(resource, info, draw_paint, placed))

    first = min(remove)
    insert_after = rp.et_after.get(rp.glyphs[first].op)
    if insert_after is None:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    block = draw_block(runs, rp.glyphs[first].ctm, None)
    instructions = _rewrite(rp, remove, insert_after, block)
    pikepdf_page = pdf.pages[page_no]
    pikepdf_page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()

    new_box = None
    if visual:
        heights = [rp.glyphs[t] for t in line.members]
        size = max(g.paint.size * g.paint.trm2[3] for g in heights)
        base = rp.glyphs[old[0].traces[0]].origin[1]
        new_box = (min(w.x for w in visual), base - 0.3 * size, max(w.x + w.width for w in visual), base + 1.0 * size)
    drawn = "".join(w.text for w in visual if w.is_new)
    _verify(src, result, page_no, line, new_text, new_box, drawn)
    return EditResult(result, warnings, new_text)


def _rewrite(rp: Replay, remove: set[int], insert_after: int, block: list) -> list:
    from .pdfcontent import remove_glyphs

    rewritten = remove_glyphs(rp, remove)
    # remove_glyphs keeps one instruction per original one, except ' and " which become
    # several; count them to find where the text object's ET ended up.
    out, k_new = [], 0
    expansions = {}
    for k, ins in enumerate(rp.instructions):
        op = str(ins.operator) if not isinstance(ins, pikepdf.ContentStreamInlineImage) else None
        touched = any(rp.glyphs[i].op == k for i in remove)
        expansions[k] = (2 if op == "'" else 4 if op == '"' else 1) if touched else 1
    index = 0
    for k in range(len(rp.instructions)):
        n = expansions[k]
        out += rewritten[index : index + n]
        index += n
        if k == insert_after:
            out += block
    return out


# Verification ----------------------------------------------------------------------------


def _render(doc_bytes: bytes, page_no: int, zoom: float = 2.0):
    with pymupdf.open("pdf", doc_bytes) as d:
        pix = d[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csGRAY)
        mu = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
    doc = pdfium.PdfDocument(doc_bytes)
    try:
        bm = doc[page_no].render(scale=zoom, grayscale=True)
        pf = bm.to_numpy().reshape(bm.height, bm.width).copy()
    finally:
        doc.close()
    return mu, pf


def _outside(a, b, box, zoom, tolerance_px):
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:h, :w].astype(int), b[:h, :w].astype(int)
    mask = np.ones((h, w), bool)
    x0, y0, x1, y1 = (v * zoom for v in box)
    mask[max(0, int(y0)) : int(y1) + 1, max(0, int(x0)) : int(x1) + 1] = False
    if tolerance_px == 0:
        changed = a != b
    else:
        def spread(img, fn):
            out = img.copy()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    out = fn(out, np.roll(np.roll(img, dy, 0), dx, 1))
            return out
        changed = (b < spread(a, np.minimum)) | (b > spread(a, np.maximum)) | (a < spread(b, np.minimum)) | (a > spread(b, np.maximum))
    return int(changed[mask].sum()), int((a != b)[~mask].sum())


def _verify(old_bytes: bytes, new_bytes: bytes, page_no: int, line: Line, new_text: str, new_box, drawn: str) -> None:
    def fail(why: str):
        raise EditRejected(f"The change couldn't be made exactly ({why}), so nothing was changed.")

    new_doc = pymupdf.open("pdf", new_bytes)
    page = new_doc[page_no]
    try:
        rp = replay(pikepdf.open(io.BytesIO(new_bytes)), page_no)
    except Unsupported:
        fail("the edited page can't be read back")
    if not matches_trace(rp, page):
        fail("the edited page can't be read back")

    # Everything outside the old and new line boxes is unchanged, in two engines.
    to_page = page.transformation_matrix
    boxes = [pymupdf.Rect(line.bbox) * page.derotation_matrix]
    if new_box:
        r = pymupdf.Rect(new_box[0], new_box[1], new_box[2], new_box[3]) * to_page
        r.normalize()
        boxes.append(r)
    area = pymupdf.Rect(boxes[0])
    for b in boxes[1:]:
        area |= b
    area = (area + (-3, -3, 3, 3)) * page.rotation_matrix
    area.normalize()
    old_mu, old_pf = _render(old_bytes, page_no)
    new_mu, new_pf = _render(new_bytes, page_no)
    outside_mu, inside_mu = _outside(old_mu, new_mu, tuple(area), 2.0, 0)
    outside_pf, inside_pf = _outside(old_pf, new_pf, tuple(area), 2.0, 1)
    if outside_mu or outside_pf:
        fail("other parts of the page would have moved")
    if new_text.strip() and (inside_mu == 0 or inside_pf == 0):
        fail("the new text wasn't visible")

    # The line reads back as typed.
    if new_text.strip():
        cands = [l for l in page_lines(page) if pymupdf.Rect(l.bbox).intersects(area)]
        if not any(comparable(l.text) == comparable(new_text) for l in cands):
            got = " | ".join(l.text for l in cands)
            fail(f"it read back as “{got}”")
        # An independent reader (PDFium) finds the same characters there.
        doc = pdfium.PdfDocument(new_bytes)
        try:
            tp = doc[page_no].get_textpage()
            pr = pymupdf.Rect(area) * page.derotation_matrix * ~page.transformation_matrix
            pr.normalize()
            found = tp.get_text_bounded(left=pr.x0, bottom=pr.y0, right=pr.x1, top=pr.y1)
        finally:
            doc.close()
        # Only the words drawn now: untouched words keep whatever labels the PDF had.
        letters = lambda t: {c for c in unicodedata.normalize("NFKC", clean_text(t)) if c.isalnum()}
        if not letters(drawn) <= letters(found):
            fail("a second PDF reader saw different letters")

    # No other page changed.
    old_pdf, new_pdf = pikepdf.open(io.BytesIO(old_bytes)), pikepdf.open(io.BytesIO(new_bytes))
    for i, (a, b) in enumerate(zip(old_pdf.pages, new_pdf.pages)):
        if i != page_no and pikepdf.Page(a).obj.Contents.read_bytes() != pikepdf.Page(b).obj.Contents.read_bytes():
            fail("another page changed")
