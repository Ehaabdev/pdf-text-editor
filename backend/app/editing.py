"""Change the text of one line, keeping its font, size, color, baseline and alignment.

How an edit works:
  1. The page's instructions are replayed (pdfcontent) and must agree with MuPDF on
     every glyph; the line's glyphs are found among them.
  2. The old line and the new text are split into words. Words that did not change
     keep their exact original glyphs. Changed words are shaped with the real font.
  3. The words are laid out the way the line was aligned (left, right, centered or
     justified). Words that end up where they already were are not touched at all;
     the others are taken out of their instructions and drawn again in a new block.
     Underlines, strikethroughs, highlights and links follow their words.
  4. The result is checked independently before it is accepted: the line reads back
     as typed (our reader and PDFium), nothing outside the line changed by a single
     pixel (MuPDF and PDFium), MuPDF sees the same shapes and links except those that
     moved with their words, and no other page changed.

Emptying a line deletes it (deleting.py).
"""

import difflib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace

import pikepdf
import pymupdf
import pypdfium2 as pdfium

from . import bidi
from . import decorations
from . import fonts as fontlib
from .changes import (
    EditRejected,
    LinkChange,
    PathChange,
    apply_links,
    check_drawings,
    check_links,
    check_other_pages,
    fail,
    ink,
    ink_box,
    new_target,
    outside,
    page_links,
    render,
    rewrite,
    to_display,
    write_page,
)
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
    page_matrix,
    replay,
)
from .textlayout import Line, made_by_word, page_lines

log = logging.getLogger("pdfedit.editing")

_BIDI_CONTROLS = dict.fromkeys(map(ord, "‎‏⁦⁧⁨⁩‪‫‬‭‮؜"))
_SPACE_RUN = re.compile(r"(\s+)")


@dataclass
class EditResult:
    pdf: bytes
    warnings: list[str] = field(default_factory=list)
    text: str = ""
    # Where the page may have changed: (page, x0, y0, x1, y1) as displayed. Exactly the
    # areas the change's own check proved that nothing outside of changed.
    areas: list[tuple[int, float, float, float, float]] = field(default_factory=list)
    # The same, drawn tight for people to see: the old and new lines' boxes.
    marks: list[tuple[int, float, float, float, float]] = field(default_factory=list)
    # Fonts that weren't available and the look-alikes used instead, for the user to
    # check: {"font", "substitute", "sameWidths"}.
    lookalikes: list[dict] = field(default_factory=list)
    # Lines moved down (to make room for a new line) or up (into a deleted line's space).
    moved: int = 0


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
    char_traces: list[int] = field(default_factory=list)  # the glyph drawing each character
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
    # Drawn by a look-alike whose letters aren't as wide as the original's (an Arabic one),
    # at this size relative to the original's: its letters can be taller or shorter at
    # the same point size. The line's own letter spacing then doesn't apply.
    other_widths: bool = False
    scale: float = 1.0
    group: int = -1  # old words between the same two spaces share a group ("review" and ".")
    # A word added between old ones: the old word after it (an underline continues
    # through an added word only if it runs under the words on both sides).
    inserted: bool = False
    after: "Word | None" = None

    @property
    def is_new(self) -> bool:
        return not self.traces


_BRACKET = set("()[]{}<>«»‹›")


def _edge(ch: str) -> bool:
    """Punctuation that separates from a word's edge: in mixed Arabic/English text it
    takes the line's direction, not the word's (Word puts "الاسم:" in an English line
    as Arabic word + colon on the right). Number signs stay: "+966", "-10", "15%"."""
    return ch in _BRACKET or unicodedata.bidirectional(ch) in ("CS", "ON", "S", "B")


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
        while word and _edge(word[0]) and len(word) > 1:
            pieces.append(word[0])
            word = word[1:]
        tail = []
        while word and _edge(word[-1]) and len(word) > 1:
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
    groups = [0]

    def add(indices):
        text = "".join(line.glyph_text(i) for i in indices)
        traces = [t for i in indices for t in line.glyphs[i].traces]
        extents = [_extent(rp.glyphs[t]) for t in traces]
        chars = [line.glyphs[i].traces[0] for i in indices for _ in line.glyph_text(i)]
        words.append(Word(text, _script(text), traces, chars, min(e[0] for e in extents), max(e[1] for e in extents)))
        words[-1].group = groups[0]

    def flush():
        if not current:
            return
        # Brackets at either end are words of their own, as in _split_words.
        def edge(i):
            t = line.glyph_text(i)
            return len(t) == 1 and _edge(t)

        head = []
        while len(current) > 1 and edge(current[0]):
            head.append(current.pop(0))
        tail = []
        while len(current) > 1 and edge(current[-1]):
            tail.insert(0, current.pop())
        for i in head:
            add([i])
        add(current)
        for i in tail:
            add([i])
        words[-1].sep = " "  # a space follows the group; its parts are joined
        current.clear()
        groups[0] += 1

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


def _looks(word: Word, line: Line, rp: Replay) -> tuple[list[int], list[int]]:
    """How an old word's characters look: per character, the index of its look, and
    one glyph showing each look. A superscript "rd", small capitals or a red letter
    inside a word make more than one."""
    style_of = {g.traces[0]: g.style for g in line.glyphs if g.traces}

    def same(a, b):
        da, db = rp.glyphs[a], rp.glyphs[b]
        pa, pb = da.paint, db.paint
        sa, sb = style_of.get(a), style_of.get(b)
        size = pa.size * pa.trm2[3]
        # Colors as the reader resolved them: one color can be written several ways.
        strokes = pa.Tr in (1, 2, 5, 6) and pa.stroke != pb.stroke
        return (
            (sa and sa.font, sa and sa.color) == (sb and sb.font, sb and sb.color)
            and abs(size - pb.size * pb.trm2[3]) < 0.05
            and abs(pa.Tz - pb.Tz) < 1e-3
            and pa.Tr == pb.Tr
            and not strokes
            and abs(da.origin[1] - db.origin[1]) < 0.1 * size
        )

    looks, kinds = [], []
    for t in word.char_traces:
        k = next((n for n, r in enumerate(kinds) if same(r, t)), None)
        if k is None:
            kinds.append(t)
            k = len(kinds) - 1
        looks.append(k)
    return looks, kinds


def _pieces(word: Word, line: Line, rp: Replay) -> list[Word]:
    """A new word replacing one old word whose letters differ in look keeps those looks.
    Where the look changes between digits and letters ("3rd", "CO2"), a new word with
    the same pattern keeps it run by run ("21st", "CO3"); any other word ("third") gets
    the look of the old word's first letter. Otherwise (small capitals, one red letter)
    each new letter takes the look of the old letter it lines up with, and an added
    letter that of the letter before it. Each run of one look becomes a piece of its
    own, joined to the next."""
    src = word.source
    looks, kinds = _looks(src, line, rp)
    if len(kinds) <= 1:
        return [word]
    if word.script == "R" or src.script == "R":
        # Arabic letters join, so the word can't be cut into separately drawn pieces.
        raise EditRejected("This Arabic word mixes sizes, colors or heights inside it, and changing it isn't possible yet.")
    runs, start = [], 0
    for i in range(1, len(looks) + 1):
        if i == len(looks) or looks[i] != looks[start]:
            runs.append((src.text[start:i], looks[start]))
            start = i
    digits = lambda t: all(c.isdecimal() for c in t)
    letters = lambda t: not any(c.isdecimal() for c in t)
    at_digits = all(digits(t) or letters(t) for t, _ in runs) and all(
        digits(a) != digits(b) for (a, _), (b, _) in zip(runs, runs[1:])
    )
    if at_digits:
        new_runs = re.findall(r"\d+|\D+", word.text)
        same_pattern = len(new_runs) == len(runs) and all(digits(a) == digits(b) for a, (b, _) in zip(new_runs, runs))
        picked = [look for t, (_, look) in zip(new_runs, runs) for _ in t] if same_pattern else [looks[0]] * len(word.text)
    else:
        picked = [0] * len(word.text)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, src.text, word.text, autojunk=False).get_opcodes():
            for j in range(j1, j2):
                i = i1 + (j - j1) * (i2 - i1) // (j2 - j1) if i2 > i1 else max(i1 - 1, 0)
                picked[j] = looks[i]
    pieces, start = [], 0
    for j in range(1, len(word.text) + 1):
        if j == len(word.text) or picked[j] != picked[start]:
            t = kinds[picked[start]]
            source = replace(src, traces=[t] + [x for x in src.traces if x != t])
            text = word.text[start:j]
            pieces.append(Word(text, _script(text), source=source, sep=word.sep if j == len(word.text) else ""))
            start = j
    return pieces


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


def _glyph_top(tt, gid: int) -> float | None:
    """How far a glyph rises above the baseline, in ems (TrueType outlines only)."""
    try:
        glyph = tt["glyf"][tt.getGlyphOrder()[gid]]
    except (IndexError, KeyError):
        return None
    if glyph.numberOfContours == 0 or not hasattr(glyph, "yMax"):
        return None
    return glyph.yMax / tt["head"].unitsPerEm if glyph.yMax > 0 else None


def _base_name_from(font_name: str) -> str:
    """A PDF font name as people know it: "TimesNewRomanPS-BoldMT" -> "Times New Roman Bold"."""
    name = font_name.split("+")[-1]
    family, _, style = name.partition(",") if "," in name else name.partition("-")
    for suffix in ("PSMT", "MT", "PS"):
        if family.endswith(suffix) and len(family) > len(suffix) + 2:
            family = family[: -len(suffix)]
        if style.endswith(suffix):
            style = style[: -len(suffix)]
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family)
    style = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", style)
    shown = f"{spaced} {style}".strip() if style and style.lower() != "regular" else spaced
    return shown[: -len(" Regular")] if shown.endswith(" Regular") else shown


class Fonts:
    def __init__(self, pdf: pikepdf.Pdf, page_index: int, rp: Replay, word_made: bool, line: Line, lines: list[Line] | None = None):
        self.pdf, self.page_index, self.rp, self.word_made, self.line = pdf, page_index, rp, word_made, line
        self.lines = lines or [line]  # the page's lines, to measure a font's size on
        self.embedded: dict[str, fontlib.EmbeddedFont] = {}
        self.warnings: list[str] = []
        self.checked: set[str] = set()
        # Fonts that weren't available and what stood in for them, for the user to check.
        self.lookalikes: dict[tuple[str, str], dict] = {}
        self._notes: dict[tuple[str, str], int] = {}  # which warning tells about each

    def _check_same_font(self, font_name: str, full: "fontlib.FullFont") -> None:
        """Warn when the installed font draws the line's existing glyphs differently
        from the copy in the PDF: another version, or not the same font at all."""
        if font_name in self.checked:
            return
        self.checked.add(font_name)
        gids = {g.gid for g in self.line.glyphs if g.style.font == font_name and g.gid > 0 and not g.is_space}
        compared = differ = 0
        for resource in self._resources_named(font_name, ""):
            emb = self._embedded(resource)
            for gid in gids:
                if emb.has_drawn_glyph(gid):
                    compared += 1
                    differ += not emb.has_same_glyph(gid, full)
        if compared and differ:
            self.warnings.append(
                f"The installed {font_name} isn't exactly the version in this PDF, so the changed words may look slightly different."
            )

    def _resources_named(self, name: str, prefer: str) -> list[str]:
        names = [r for r, info in self.rp.fonts.items() if info is not None and _base_name(info.obj) == name]
        return sorted(names, key=lambda r: (r != prefer, not self.rp.fonts[r].two_byte))

    def _embedded(self, resource: str) -> fontlib.EmbeddedFont:
        if resource not in self.embedded:
            self.embedded[resource] = fontlib.EmbeddedFont(self.rp.fonts[resource].obj)
        return self.embedded[resource]

    def shape(self, word: Word, paint: Paint, font_name: str) -> None:
        """Shape a new word and choose the font that will draw it, in this order: the
        installed font itself; for Latin, the PDF's embedded copy when it has every
        letter; else a stand-in (the font's look-alike, or one of its kind), whose
        letters the user is asked to check. Nothing is refused for want of a font, only
        a character no font here has."""
        text = word.text
        entry = fontlib.library().find(font_name)
        full = fontlib.load_full_font(entry) if entry else None
        candidates = self._resources_named(font_name, paint.font)
        needs_shaping = word.script == "R" or any(unicodedata.combining(c) for c in text)

        if full is not None:
            shaped = full.shape(text, word.rtl, latin_ligatures=not self.word_made, kerning=not self.word_made)
            if not any(gid == 0 for gid, *_ in shaped):
                self._check_same_font(font_name, full)
                word.shaped = shaped
                gids = [g[0] for g in shaped]
                for resource in candidates:
                    codes = self._codes_in_copy(resource, gids, shaped, text, full)
                    if codes is not None:
                        word.choice = FontChoice(resource, full, self.rp.fonts[resource], codes)
                        return
                if fontlib.embeddable(full):
                    word.choice = FontChoice(None, full, None, gids)
                    return
            # The installed font lacks some of these letters, or its licence keeps it out
            # of PDFs: as if it weren't installed.

        if not needs_shaping and self._from_copy(word, candidates):
            return
        self._shape_stand_in(word, font_name)

    def _from_copy(self, word: Word, candidates: list[str]) -> bool:
        """Latin text drawn with the PDF's embedded copy of its font, if it has every letter."""
        text = word.text
        for resource in candidates:
            info, emb = self.rp.fonts[resource], self._embedded(resource)
            # Which code draws a character: the copy's own labels (subsets often have no
            # character table of their own), else its character table.
            by_label = {}
            for code, label in sorted(info.to_unicode.items()):
                by_label.setdefault(label, code)
            shaped, codes = [], []
            for k, ch in enumerate(text):
                code = by_label.get(ch)
                if code is None:
                    gid = emb.gid_for(ch)
                    code = gid if info.two_byte else _winansi(ch)
                gid = code if info.two_byte else emb.gid_for(ch)
                drawn = emb.tt is None or (gid is not None and emb.has_drawn_glyph(gid)) or ch.isspace()
                if code is None or code not in info.widths or not drawn:
                    break
                shaped.append((gid or 0, k, info.width(code), 0.0, 0.0))
                codes.append(code)
            else:
                word.shaped = shaped
                word.choice = FontChoice(resource, None, info, codes)
                return True
        return False

    def _kind(self, font_name: str) -> tuple[bool | None, bool | None]:
        """(serif, fixed width) as the PDF describes its font; None where it doesn't."""
        for resource in self._resources_named(font_name, ""):
            obj = self.rp.fonts[resource].obj
            try:
                descriptor = obj.get("/FontDescriptor")
                if descriptor is None and "/DescendantFonts" in obj:
                    descriptor = obj.DescendantFonts[0].get("/FontDescriptor")
                if descriptor is not None and "/Flags" in descriptor:
                    flags = int(descriptor.Flags)
                    return bool(flags & 2), bool(flags & 1)
            except (AttributeError, IndexError, TypeError, ValueError):
                continue
        return None, None

    def _shape_stand_in(self, word: Word, font_name: str) -> None:
        """Shape the word with a free font that stands in: the font's look-alike, else
        one of its kind; the other if the first lacks a letter."""
        arabic = word.script == "R"
        serif, fixed = self._kind(font_name)
        first = fontlib.stand_in(font_name, arabic, serif, fixed)
        tries = [first]
        if first.close:
            tries.append(fontlib.stand_in(font_name, arabic, serif, fixed, generic=True))
        for sub in tries:
            full = fontlib.load_full_font(sub.entry)
            shaped = full.shape(word.text, word.rtl, latin_ligatures=not self.word_made, kerning=not self.word_made)
            missing = [word.text[c] for gid, c, *_ in shaped if gid == 0]
            if not missing:
                break
        else:
            raise EditRejected(f"“{missing[0]}” can't be drawn: the PDF's font doesn't have it, and no other font here does either.")
        word.shaped = shaped
        word.choice = FontChoice(None, full, None, [g[0] for g in shaped])  # always embedded anew
        word.other_widths = not sub.same_widths
        word.scale = 1.0 if sub.same_widths else self._size_match(font_name, full, word.script)
        self._note_stand_in(font_name, sub, word.text)

    def _note_stand_in(self, font_name: str, sub: "fontlib.Lookalike", drawn: str) -> None:
        """Tell the user which letters a stand-in drew, to check them: one note per font."""
        shown = _base_name_from(font_name)
        key = (shown, sub.name)
        entry = self.lookalikes.get(key)
        if entry is None:
            entry = self.lookalikes[key] = {"font": shown, "substitute": sub.name, "sameWidths": sub.same_widths, "letters": ""}
            self.warnings.append("")
            self._notes[key] = len(self.warnings) - 1
        for ch in drawn:
            if not ch.isspace() and ch not in entry["letters"]:
                entry["letters"] += ch
        letters = entry["letters"]
        listed = "، ".join(letters) if any(_script(c) == "R" for c in letters) else ", ".join(letters)
        how = "which looks almost the same" if sub.same_widths else "a similar font"
        self.warnings[self._notes[key]] = (
            f"Some new letters ({listed}) aren't in this PDF's {shown}, so they're drawn in {sub.name}, {how}. "
            "Check that they look right."
        )

    def _size_match(self, font_name: str, full: "fontlib.FullFont", script: str = "R") -> float:
        """How big the look-alike must be drawn to match the original's letters. Best:
        the line's own Arabic words in that font, as wide as the look-alike sets them
        (how people judge size along a line). Else (a stretched kashida line, too few
        letters): the height of the tall letters (alef, lam, kaf, ta) in the PDF's own
        copy of the font against the look-alike's, the middle of the ratios."""
        by_width = self._width_ratio(font_name, full, script)
        if by_width is not None:
            return min(1.4, max(0.6, by_width))
        return self._height_ratio(font_name, full, script)

    def _width_ratio(self, font_name: str, full: "fontlib.FullFont", script: str = "R") -> float | None:
        """Measured on the line itself, else on the page's other lines in that font too
        (a heading of one word); never on lines stretched with kashida."""
        actual = shaped = 0.0
        chars = 0
        others = [l for l in self.lines if l is not self.line and any(g.style.font == font_name for g in l.glyphs)]
        for line in [self.line, *others]:
            if chars >= 6 and line is not self.line:
                break
            if line.kashida:
                continue
            a, s, c = self._widths_on(line, font_name, full, script)
            actual, shaped, chars = actual + a, shaped + s, chars + c
        if chars < 6 or shaped <= 0:
            return None
        return actual / shaped

    def _widths_on(self, line: Line, font_name: str, full: "fontlib.FullFont", script: str = "R") -> tuple[float, float, int]:
        """The line's words in this font and script: their width on the page, and set in `full`."""
        actual = shaped = 0.0
        chars = 0
        for w in _old_words(line, self.rp):
            if (w.script == "R") != (script == "R") or w.script not in ("R", "L") or _style_of(line, w) != font_name or len(_looks(w, line, self.rp)[1]) > 1:
                continue
            p = self.rp.glyphs[w.traces[0]].paint
            em = p.size * p.Tz * p.trm2[0] / 1000
            glyphs = full.shape(w.text, w.script == "R", latin_ligatures=not self.word_made, kerning=not self.word_made)
            if any(g[0] == 0 for g in glyphs):
                continue
            actual += w.u1 - w.u0
            shaped += sum(g[2] for g in glyphs) * em
            chars += len(w.text)
        return actual, shaped, chars

    def _height_ratio(self, font_name: str, full: "fontlib.FullFont", script: str = "R") -> float:
        tall = "الكطظ" if script == "R" else "HIdhklbf"
        ratios = []
        cmap = full.tt.getBestCmap() or {}
        for resource in self._resources_named(font_name, ""):
            info, emb = self.rp.fonts[resource], self._embedded(resource)
            if emb.tt is None or "glyf" not in emb.tt:
                continue
            for code, label in info.to_unicode.items():
                if label in tall and ord(label) in cmap:
                    gid = code if info.two_byte else emb.gid_for(label)
                    ours = _glyph_top(emb.tt, gid) if gid is not None else None
                    theirs = _glyph_top(full.tt, full.tt.getGlyphID(cmap[ord(label)]))
                    if ours and theirs:
                        ratios.append(ours / theirs)
        if not ratios:
            return 1.0
        ratios.sort()
        return min(1.4, max(0.6, ratios[len(ratios) // 2]))

    def _codes_in_copy(self, resource, gids, shaped, text, full) -> list[int] | None:
        """Codes to draw the glyphs with this embedded copy, if it has all of them drawn
        identically to the installed font."""
        info, emb = self.rp.fonts[resource], self._embedded(resource)
        if emb.tt is None:
            return None
        texts = _glyph_texts(text, shaped, full)
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
            try:
                font = fontlib.embed_font(self.pdf, full, used)
            except fontlib.EmbeddingNotAllowed:
                raise EditRejected(
                    f"The licence of the font {full.entry.ps_name} doesn't allow putting it in a PDF, so new letters can't be added in it."
                )
            cache[key] = str(pikepdf.Page(self.pdf.pages[self.page_index]).add_resource(font, pikepdf.Name.Font, prefix="FE"))
        return cache[key]


def _winansi(ch: str) -> int | None:
    try:
        b = ch.encode("cp1252")
    except UnicodeEncodeError:
        return None
    return b[0] if len(b) == 1 else None


def _glyph_texts(text: str, shaped, full: "fontlib.FullFont | None" = None) -> list[str]:
    """For copy and search: the text each new glyph stands for. A ligature gets all its
    letters; a vowel mark drawn as its own glyph gets just the mark. In right-to-left
    output a mark's glyph comes before its letter's, so glyphs are told apart by what
    the font says they are (or, failing that, marks have no advance)."""
    clusters = sorted({c for _, c, *_ in shaped})
    span = {c: text[c : (clusters[i + 1] if i + 1 < len(clusters) else len(text))] for i, c in enumerate(clusters)}
    by_gid = full.char_of_gid() if full is not None else {}
    out: list[str] = [""] * len(shaped)
    for c in clusters:
        members = [k for k, g in enumerate(shaped) if g[1] == c]
        piece = span[c]
        marks = [ch for ch in piece if unicodedata.combining(ch)]
        letters = "".join(ch for ch in piece if not unicodedata.combining(ch))

        def is_mark(k):
            ch = by_gid.get(shaped[k][0])
            if ch is not None:
                return bool(unicodedata.combining(ch))
            return shaped[k][2] == 0 and len(members) > 1

        mark_glyphs = [k for k in members if is_mark(k)]
        base_glyphs = [k for k in members if k not in mark_glyphs]
        for k in mark_glyphs:
            ch = by_gid.get(shaped[k][0])
            pick = ch if ch in marks else (marks[0] if marks else "")
            if pick in marks:
                marks.remove(pick)
            out[k] = pick
        if base_glyphs:
            out[base_glyphs[0]] = letters + "".join(marks)  # marks with no glyph of their own
            for k in base_glyphs[1:]:
                out[k] = ""
        elif mark_glyphs and letters:
            out[mark_glyphs[0]] = letters + out[mark_glyphs[0]]
    # A glyph must stand for something; give an empty one its cluster's first character.
    return [t or span[shaped[k][1]][:1] for k, t in enumerate(out)]


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


def _visual_order_standard(words: list[Word], rtl: bool) -> list[Word]:
    """Words in left-to-right page order by the Unicode bidi algorithm, as Chrome and
    other programs that follow it lay text out. A new word whose characters end up at
    different levels ("+966" after Arabic: the "+" goes on the other side) is split into
    joined pieces. Returns the (possibly longer) typing-order list and the visual list."""
    def text_of(ws):
        return "".join(w.text + w.sep for w in ws)

    lv = bidi.levels(text_of(words), rtl)
    split: list[Word] = []
    pos = 0
    for w in words:
        own = lv[pos : pos + len(w.text)]
        if w.is_new and len(set(own)) > 1:
            k = 0
            while k < len(w.text):
                j = k + 1
                while j < len(w.text) and own[j] == own[k]:
                    j += 1
                piece = w.text[k:j]
                last = j == len(w.text)
                split.append(Word(piece, _script(piece), source=w.source, sep=w.sep if last else ""))
                k = j
        else:
            split.append(w)
        pos += len(w.text) + len(w.sep)
    for k, w in enumerate(split):
        w.logical = k

    lv = bidi.levels(text_of(split), rtl)
    starts, pos = [], 0
    for w in split:
        starts.append(pos)
        pos += len(w.text) + len(w.sep)
    rank = {i: r for r, i in enumerate(bidi.visual_order(lv))}
    for w, s in zip(split, starts):
        w.rtl = lv[s] % 2 == 1
    visual = sorted(split, key=lambda w: min(rank[starts[w.logical] + k] for k in range(len(w.text))))
    return split, visual


def _pdf_rect(bbox, page: pymupdf.Page) -> pymupdf.Rect:
    """A rectangle in displayed page space, in PDF coordinates (x as in the content stream)."""
    r = pymupdf.Rect(bbox) * (page.derotation_matrix * ~page_matrix(page))
    r.normalize()
    return r


def _room(line: Line, lines: list[Line], page: pymupdf.Page, column, u0: float, u1: float) -> tuple[float, float]:
    """How far the line may reach (PDF x). A table cell's borders are firm. Without
    one, the column edges are only the page's outermost text, which on a page of short
    lines is the longest of them; side margins are then taken to mirror each other,
    as in most documents, so a short line can still grow."""
    left, right = column
    paper = _pdf_rect(page.rect, page)
    # Columns are measured as the page was written, before its rotation.
    edges = [pymupdf.Rect(l.column[0], l.written[1], l.column[1], l.written[3]) * ~page_matrix(page) for l in lines if l.column]
    for r in edges:
        r.normalize()
    if edges:
        text_left, text_right = min(r.x0 for r in edges), max(r.x1 for r in edges)
        if right > text_right - 0.5:
            right = max(right, paper.x1 - (text_left - paper.x0))
        if left < text_left + 0.5:
            left = min(left, paper.x0 + (paper.x1 - text_right))
    return min(left, u0) - 0.5, max(right, u1) + 0.5


def _collides(line: Line, lines: list[Line], page: pymupdf.Page, old: tuple[float, float], new: tuple[float, float]) -> bool:
    """Whether the new extent (PDF x) reaches text or an image beside the line that the
    old one stayed clear of: a second column, a value on the same row, a picture."""
    me = _pdf_rect(line.bbox, page)
    others = [_pdf_rect(l.bbox, page) for l in lines if l is not line]
    others += [_pdf_rect(i["bbox"], page) for i in page.get_image_info()]
    for r in others:
        if min(r.y1, me.y1) - max(r.y0, me.y0) <= 0.3 * min(r.height, me.height):
            continue  # another row
        if r.x0 < old[1] and r.x1 > old[0]:
            continue  # beside the old text already (underneath it, or a background)
        if r.x0 < new[1] + 1 and r.x1 > new[0] - 1:
            return True
    return False


def _paragraph_edges(line: Line, lines: list[Line], page: pymupdf.Page) -> list[tuple[float, float]]:
    """Left and right ink edges (PDF x) of the nearby lines in the same column: the
    lines just above and below that overlap this one, i.e. probably its paragraph."""
    to_pdf = page.derotation_matrix * ~page_matrix(page)
    def edges(l):
        r = pymupdf.Rect(l.bbox) * to_pdf
        r.normalize()
        return r
    me = edges(line)
    height = me.height or 1.0
    out = []
    for other in lines:
        if other is line:
            continue
        r = edges(other)
        overlap = min(r.x1, me.x1) - max(r.x0, me.x0)
        if overlap > 0.5 * min(r.width, me.width) and abs(r.y0 - me.y0) < 3 * height and abs(r.height - height) < 0.5 * height:
            out.append((r.x0, r.x1))
    return out


def _alignment(line: Line, old: list[Word], column: tuple[float, float], space: float, neighbours) -> str:
    u0, u1 = old[0].u0, old[-1].u1
    gaps = [b.u0 - a.u1 for a, b in zip(old, old[1:]) if b.u0 - a.u1 > 0.3 * space]
    if line.kashida:
        return "justify"
    if neighbours:
        # The paragraph's edges are where its lines end. A line reaching both, when at
        # least one other line shares the edge it would otherwise be ragged on, is justified.
        left = min([u0] + [a for a, _ in neighbours])
        right = max([u1] + [b for _, b in neighbours])
        at_left, at_right = u0 - left < 1.5, right - u1 < 1.5
        ragged_side = [b for _, b in neighbours] if not line.rtl else [a for a, _ in neighbours]
        edge = right if not line.rtl else left
        shared = any(abs(x - edge) < 1.5 for x in ragged_side)
        if at_left and at_right and shared and gaps:
            return "justify"
        lefts_vary = any(abs(a - left) > 1.5 for a, _ in neighbours)
        rights_vary = any(abs(b - right) > 1.5 for _, b in neighbours)
        centres = [(a + b) / 2 for a, b in neighbours]
        if lefts_vary and rights_vary and all(abs(c - (u0 + u1) / 2) < 2 for c in centres):
            return "center"
        return "right" if line.rtl else "left"
    left, right = column
    # Within one space of an edge: the column's edge can include a trailing space.
    touches_left, touches_right = u0 - left < 2.5 + space, right - u1 < 2.5 + space
    if touches_left and touches_right and gaps:
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
    all_lines = page_lines(page)
    line = next((l for l in all_lines if l.id == line_id), None)
    if line is None or comparable(line.text) != comparable(old_text):
        raise EditRejected("This line changed since you selected it. Select it again.")
    if comparable(new_text) == comparable(line.text) and not redraw_all:
        return EditResult(src, [], line.text)
    if not comparable(new_text):
        from .deleting import delete_lines

        return delete_lines(src, [(page_no, line_id, old_text)])
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
    to_pdf = ~page_matrix(page)
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
    # Words drawn over each other (Word puts an Arabic word's bracket on its far side)
    # can't be laid out word by word. Kerning that tucks "." under "w" doesn't count.
    tangled = any(
        b.u0 < a.u1 - (0.15 * line.style.size if a.group == b.group else 0.5) for a, b in zip(old_visual, old_visual[1:])
    )
    # Some lines are drawn again in full, unchanged words too.
    redraw = redraw_all or tangled or line.kashida
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal" and not redraw:
            for k in range(i2 - i1):
                old[i1 + k].sep = new_pairs[j1 + k][1]
                words.append(old[i1 + k])
            continue
        for j in range(j1, j2):
            source = old[min(i1 + (j - j1), i2 - 1)] if i2 > i1 else (old[i1 - 1] if i1 > 0 else old[0])
            word = Word(new_texts[j], _script(new_texts[j]), source=source, sep=new_pairs[j][1])
            if i2 == i1:
                word.inserted, word.after = True, (old[i1] if i1 < len(old) else None)
            # A word that replaces exactly one old word keeps its letters' looks; others
            # (two words for one, an added word) take their source's main look.
            words += _pieces(word, line, rp) if i2 - i1 == j2 - j1 else [word]
    for k, w in enumerate(words):
        w.logical = k

    if word_made:
        visual = _visual_order(words, line.rtl)
    else:
        words, visual = _visual_order_standard(words, line.rtl)
    fonts = Fonts(pdf, page_no, rp, word_made, line, all_lines)
    tracking: dict[str, float] = {}
    for w in visual:
        if w.is_new:
            src_glyph = rp.glyphs[w.source.traces[0]]
            style = _style_of(line, w.source)
            fonts.shape(w, src_glyph.paint, style)
            em = src_glyph.paint.size * src_glyph.paint.Tz * src_glyph.paint.trm2[0] / 1000 * w.scale
            if w.choice.full is not None and not w.other_widths:
                if style not in tracking:
                    tracking[style] = _tracking(line, old, rp, style, w.choice.full, word_made)
                r = tracking[style]
                w.shaped = [(gid, c, adv * r, dx * r, dy) for gid, c, adv, dx, dy in w.shaped]
            elif w.choice.full is None:
                # Drawn from the PDF's own copy (no installed font): the same spacing.
                key = f"copy:{style}"
                if key not in tracking:
                    tracking[key] = _tracking_from_copy(line, old, rp, style)
                r = tracking[key]
                w.shaped = [(gid, c, adv * r, dx * r, dy) for gid, c, adv, dx, dy in w.shaped]
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
        # The words on the page these stand for (themselves, or the words they replace).
        pa, pb = (a if not a.is_new else a.source), (b if not b.is_new else b.source)
        if pa is not None and pb is not None and pa is not pb and pb.visual == pa.visual + 1 and not (is_joined and (a.is_new or b.is_new)):
            # Neighbours on the page keep their gap: a list's tab, a justified space.
            gaps.append(pb.u0 - pa.u1)
            is_joined = is_joined or pb.u0 - pa.u1 < 0.3 * space  # touching on the page
        elif is_joined:
            gaps.append(0.0)
        else:
            gaps.append(typical)
        joined.append(is_joined)

    mode = _alignment(line, old_visual, column, space, _paragraph_edges(line, all_lines, page))
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
    room = _room(line, all_lines, page, column, u0, u1)
    nx0, nx1 = visual[0].x, visual[-1].x + visual[-1].width
    if nx0 < room[0] or nx1 > room[1]:
        log.info("too long: %s", layout_facts())
        raise EditRejected("The new text is too long to fit on this line. Shorten it a little.")
    if _collides(line, all_lines, page, (u0, u1), (nx0, nx1)):
        log.info("collides: %s", layout_facts())
        raise EditRejected("The new text would run into other text on the page. Shorten it a little.")

    warnings = list(fonts.warnings)
    if line.kashida:
        warnings.append("This line was stretched with Arabic kashida; it is now justified with wider spaces instead.")
    return _apply(src, pdf, mu, rp, page_no, line, old, visual, column, word_made, warnings, new_text, gaps=gaps, joined=joined, fonts=fonts, space=space)


def _tracking(line: Line, old: list[Word], rp: Replay, font_name: str, full: "fontlib.FullFont", word_made: bool) -> float:
    """How tightly this line's words in this font were spaced, compared with the font's
    own widths: Word rounds letter positions, which makes lines a fraction of a percent
    tighter or looser. New words get the same, so they sit like the rest of the line."""
    shaped_total = actual_total = 0.0
    chars = 0
    for w in old:
        if _style_of(line, w) != font_name or not w.text.strip() or len(_looks(w, line, rp)[1]) > 1:
            continue
        d = rp.glyphs[w.traces[0]]
        em = d.paint.size * d.paint.Tz * d.paint.trm2[0] / 1000
        shaped = full.shape(w.text, w.script == "R", latin_ligatures=not word_made, kerning=not word_made)
        if any(g[0] == 0 for g in shaped):
            continue
        shaped_total += sum(g[2] for g in shaped) * em
        actual_total += w.u1 - w.u0
        chars += len(w.text)
    if chars < 4 or shaped_total <= 0:
        return 1.0
    return min(1.03, max(0.97, actual_total / shaped_total))


def _tracking_from_copy(line: Line, old: list[Word], rp: Replay, font_name: str) -> float:
    """The line's spacing as _tracking measures it, without an installed font: the
    words' extents against the widths the PDF stores for their glyphs."""
    actual = nominal = 0.0
    chars = 0
    for w in old:
        if _style_of(line, w) != font_name or not w.text.strip() or len(_looks(w, line, rp)[1]) > 1:
            continue
        glyphs = [rp.glyphs[t] for t in w.traces]
        p = glyphs[0].paint
        em = p.size * p.Tz * p.trm2[0] / 1000
        nominal += sum(rp.fonts[g.paint.font].width(g.code) for g in glyphs) * em
        actual += w.u1 - w.u0
        chars += len(w.text)
    if chars < 4 or nominal <= 0:
        return 1.0
    return min(1.03, max(0.97, actual / nominal))


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


def _apply(src, pdf, mu, rp, page_no, line, old, visual, column, word_made, warnings, new_text, gaps=None, joined=None, fonts=None, space=0.0):
    # Words that land exactly where they were stay untouched, but only those left of the
    # first word drawn again: other programs read a line in drawing order, so the kept
    # words and the new block must follow each other left to right. (A word drawn again
    # in its old place, with its old glyphs, looks exactly the same.)
    in_place = {id(w) for w in visual if not w.is_new and abs(w.x - w.u0) < 1e-4}
    redrawn_x = [w.x for w in visual if id(w) not in in_place]
    first_redrawn = min(redrawn_x) if redrawn_x else float("inf")
    untouched = {id(w) for w in visual if id(w) in in_place and w.x < first_redrawn}
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
            for (gid, *_), text in zip(w.shaped, _glyph_texts(w.text, w.shaped, choice.full)):
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
        size = paint.size * w.scale
        draw_paint = Paint(resource, size, paint.Tz, paint.Tr, paint.trm2, paint.fill, paint.stroke, paint.extgstates)
        em_x = size * paint.Tz * paint.trm2[0] / 1000
        em_y = size * paint.trm2[3] / 1000
        pen, placed = 0.0, []
        baseline = src_glyph.origin[1]
        for (gid, cluster, adv, dx, dy), code in zip(w.shaped, w.choice.codes):
            placed.append(PlacedGlyph(code, (w.x + (pen + dx) * em_x, baseline + dy * em_y)))
            pen += adv
        runs.append(PlacedRun(resource, info, draw_paint, placed))

    runs += _spaces(line, rp, visual, untouched, joined or [])

    # Underlines, strikethroughs and highlights over moved or changed words follow them.
    path_changes = []
    for d in decorations.for_line(line, rp):
        if not d.traces & remove:
            continue
        spans = _follow(d.traces, visual, rp)
        if len(spans) == 1 and abs(spans[0][0] - d.x0) < 0.005 and abs(spans[0][1] - d.x1) < 0.005:
            continue
        if d.shared:
            raise EditRejected("An underline on this line is drawn together with other shapes, so it can't be changed safely yet.")
        path_changes.append(PathChange(d.path, (d.x0, d.x1), spans))
    link_changes = _link_changes(pdf, page_no, rp, line, visual, remove, warnings)

    # Draw the block next to the line's own visible letters: hidden members (a cell's
    # paragraph mark) can sit in a neighbouring cell, whose clipping would hide the text.
    ink_traces = [t for w in old for t in w.traces if t in remove]
    first = min(ink_traces) if ink_traces else min(remove)
    insert_after = rp.et_after.get(rp.glyphs[first].op)
    if insert_after is None:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    block = draw_block(runs, rp.glyphs[first].ctm, None)
    write_page(pdf, page_no, rewrite(rp, remove, insert_after, block, path_changes))
    apply_links(pdf, page_no, link_changes)
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()

    heights = [rp.glyphs[t] for t in line.members]
    size = max(g.paint.size * g.paint.trm2[3] for g in heights)
    base = rp.glyphs[old[0].traces[0]].origin[1]
    boxes = [(min(w.x for w in visual), base - 0.3 * size, max(w.x + w.width for w in visual), base + 1.0 * size)]
    for c in path_changes:
        x0, y0, x1, y1 = rp.paths[c.path].bbox
        boxes += [(x0, y0, x1, y1)] + [(a, y0, b, y1) for a, b in c.spans]
    drawn = "".join(w.text for w in visual if w.is_new)
    area, mark = _verify(src, result, page_no, line, new_text, boxes, drawn, rp, path_changes, link_changes)
    return EditResult(result, warnings, new_text, [(page_no, *area)], [(page_no, *mark)], list(fonts.lookalikes.values()))


def _spaces(line: Line, rp: Replay, visual: list[Word], untouched: set[int], joined: list[bool]) -> list[PlacedRun]:
    """Real space glyphs in the gaps next to redrawn words, as Word draws them, so other
    programs copy the line with its spaces. The line's own space glyph is reused: the one
    from that gap if the two words were neighbours, else any from the line."""
    originals = [t for g in line.glyphs if g.is_space and not g.synthetic for t in g.traces]
    if not originals:
        return []
    runs = []
    for k, (a, b) in enumerate(zip(visual, visual[1:])):
        if (id(a) in untouched and id(b) in untouched) or (k < len(joined) and joined[k]):
            continue
        start, end = a.x + a.width, b.x
        between = [t for t in originals if not a.is_new and not b.is_new and a.u1 - 0.01 <= rp.glyphs[t].origin[0] <= b.u0 + 0.01]
        d = rp.glyphs[(between or originals)[0]]
        advance = d.advance / 1000 * d.paint.size * d.paint.Tz * d.paint.trm2[0]
        if end - start < 0.6 * advance:
            continue  # too tight for a space glyph; readers see the gap
        # On the space's own baseline: next to a superscript the word's would be raised.
        runs.append(PlacedRun(d.paint.font, rp.fonts[d.paint.font], d.paint, [PlacedGlyph(d.code, (start, d.origin[1]))]))
    return runs




def _follow(covered: frozenset[int], visual: list[Word], rp: Replay) -> list[tuple[float, float]]:
    """Where something over these glyphs (an underline, a link) goes after the edit: one
    span (PDF x) per run of neighbouring words that carry it. A changed word carries it
    if the word it replaces did; an added word, only if the words on both sides do."""
    pieces = []
    for v, w in enumerate(visual):
        if w.is_new:
            source = set(w.source.traces) if w.source is not None else set()
            if not source & covered:
                continue
            if w.inserted and not (w.after is not None and set(w.after.traces) & covered):
                continue
            if source - covered:
                raise EditRejected(
                    "Part of a word on this line is underlined, struck out, highlighted or a link. Changing that word isn't possible yet."
                )
            pieces.append((v, w.x, w.x + w.width))
        else:
            hit = set(w.traces) & covered
            if not hit:
                continue
            dx = w.x - w.u0
            ends = [_extent(rp.glyphs[t]) for t in hit]
            pieces.append((v, min(a for a, _ in ends) + dx, max(b for _, b in ends) + dx))
    spans: list[list] = []
    for v, a, b in sorted(pieces):
        if spans and spans[-1][0] == v - 1:
            spans[-1][0], spans[-1][2] = v, max(spans[-1][2], b)
        else:
            spans.append([v, a, b])
    return [(a, b) for _, a, b in spans]


def _link_changes(pdf, page_no, rp, line, visual, remove, warnings) -> list[LinkChange]:
    """Links over the line's changed words: the clickable area follows the words, and a
    link to its own text ("mailto:" plus the address) gets the new text as its target."""
    members = set(line.members)
    changes = []
    for link in page_links(pdf, page_no, rp):
        if not link.glyphs & remove:
            continue
        if not link.glyphs <= members or not link.single_quad:
            warnings.append("A link on this line also covers other text, so it wasn't changed.")
            continue
        spans = _follow(link.glyphs, visual, rp)
        if not spans:
            changes.append(LinkChange(link, None))
            continue
        # The clickable area keeps its margins around the text.
        x0, y0, x1, y1 = link.rect
        left = min(_extent(rp.glyphs[t])[0] for t in link.glyphs)
        right = max(_extent(rp.glyphs[t])[1] for t in link.glyphs)
        rect = (spans[0][0] - (left - x0), y0, spans[-1][1] + (x1 - right), y1)
        old_text = "".join(
            line.glyph_text(i)
            for i in line.order
            if line.glyphs[i].traces and line.glyphs[i].traces[0] in link.glyphs and not line.glyphs[i].is_space
        )
        carried = [
            w for w in sorted(visual, key=lambda w: w.logical)
            if any(a - 0.01 <= w.x and w.x + w.width <= b + 0.01 for a, b in spans)
        ]
        new_text = "".join(w.text + (w.sep if k < len(carried) - 1 else "") for k, w in enumerate(carried)).strip()
        uri = None
        if new_text != old_text:
            uri = new_target(link.uri, old_text, new_text)
            if uri is None and link.uri:
                warnings.append(f"The link on “{new_text}” still opens {link.uri}.")
        changes.append(LinkChange(link, rect, uri))
    return changes


# Verification ----------------------------------------------------------------------------


def _verify(old_bytes: bytes, new_bytes: bytes, page_no: int, line: Line, new_text: str, boxes, drawn: str,
            rp: Replay, path_changes: list[PathChange], link_changes: list[LinkChange]) -> tuple[tuple, tuple]:
    """Check the edit; return the area (as displayed) outside which nothing changed, and
    the old and new lines' boxes together, to show people."""
    new_doc = pymupdf.open("pdf", new_bytes)
    page = new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new_bytes)), page_no)
    except Unsupported:
        fail("the edited page can't be read back")
    if not matches_trace(new_rp, page):
        fail("the edited page can't be read back")

    # Everything outside the old and new line (and its decorations) is unchanged, in two engines.
    area = pymupdf.Rect(line.bbox) | to_display(page, ink_box(rp, line.members))
    for b in boxes:
        area |= to_display(page, b)
    # Vowel marks can reach a little past the font's height: a third of the size.
    v = max(3.0, 0.35 * line.style.size)
    area = area + (-3, -v, 3, v)
    old_mu, old_pf = render(old_bytes, page_no)
    new_mu, new_pf = render(new_bytes, page_no)
    if outside(old_mu, new_mu, [tuple(area)], 2.0, 0) or outside(old_pf, new_pf, [tuple(area)], 2.0, 1):
        fail("other parts of the page would have moved")
    # The new text leaves ink, in both engines: text can vanish (a clipping window, a
    # wrong colour) while pixels still change.
    for before, after in ((old_mu, new_mu), (old_pf, new_pf)):
        had, has = ink(before, tuple(area), 2.0), ink(after, tuple(area), 2.0)
        if has < max(1, 0.3 * had) * (1 if had else 0) or (had == 0 and has == 0):
            fail("the new text wasn't visible")

    # The line reads back as typed.
    cands = [l for l in page_lines(page) if pymupdf.Rect(l.bbox).intersects(area)]
    mine = [l for l in cands if comparable(l.text) == comparable(new_text)]
    if not mine:
        got = " | ".join(l.text for l in cands)
        fail(f"it read back as “{got}”")
    # An independent reader (PDFium) finds the same characters there.
    doc = pdfium.PdfDocument(new_bytes)
    try:
        tp = doc[page_no].get_textpage()
        pr = pymupdf.Rect(area) * page.derotation_matrix * ~page_matrix(page)
        pr.normalize()
        found = tp.get_text_bounded(left=pr.x0, bottom=pr.y0, right=pr.x1, top=pr.y1)
    finally:
        doc.close()
    # Only the words drawn now: untouched words keep whatever labels the PDF had.
    letters = lambda t: {c for c in unicodedata.normalize("NFKC", clean_text(t)) if c.isalnum()}
    if not letters(drawn) <= letters(found):
        fail("a second PDF reader saw different letters")

    # MuPDF sees the same shapes and links, except those that moved with their words.
    old_page = pymupdf.open("pdf", old_bytes)[page_no]
    check_drawings(old_page, page, path_changes, rp)
    check_links(old_page, page, link_changes)
    check_other_pages(old_bytes, new_bytes, {page_no})
    mark = pymupdf.Rect(line.bbox) | pymupdf.Rect(mine[0].bbox)
    for c in path_changes:  # an underline or highlight that moved
        mark |= to_display(page, rp.paths[c.path].bbox)
    return tuple(area), tuple(mark)
