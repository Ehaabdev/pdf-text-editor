"""Add a new line of text, in the style of a line already on the page.

The new line takes the reference line's font, size, color and text state, and the
place a new line of its paragraph would have: one line spacing below (or above), at
the paragraph's start edge, or centered or flush to the far edge if the paragraph is.
In a list, a new item ("4. …" under "3. …") gets its number in the list's own number
font, at the number's place, and its text at the item's tab; a bullet is copied. Its
letters are shaped with the installed font exactly as for an edit (editing.py): the
font rules are the same, so new letters need the exact font.

The text is drawn at the end of the page's instructions, after the page's own drawing
state has been restored, so nothing of the reference line's clipping or position can
hide it. Nothing moves to make room: where the new line would touch other text, a
picture, a form field or a line of a table, or leave its table cell, it is refused.

Before it is accepted: every glyph already on the page is drawn exactly as before,
the new line reads back as typed and the other lines read as before (our reader), PDFium
sees its letters, nothing outside the new line changes by a single pixel (MuPDF and
PDFium), MuPDF sees the same shapes and links, and no other page changes.
"""

import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

import pikepdf
import pymupdf
import pypdfium2 as pdfium

from . import decorations
from . import fonts as fontlib
from .changes import (
    EditRejected,
    check_drawings,
    check_links,
    check_other_pages,
    fail,
    ink,
    outside,
    render,
    to_display,
)
from .editing import (
    EditResult,
    Fonts,
    Word,
    _alignment,
    _bracket_insides,
    _glyph_texts,
    _looks,
    _old_words,
    _paragraph_edges,
    _pdf_rect,
    _room,
    _script,
    _space_width,
    _split_words,
    _style_of,
    _tracking,
    _visual_order,
    _visual_order_standard,
    clean_text,
    comparable,
)
from .pdfcontent import (
    IDENTITY,
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

_RTL = {"R", "AL"}
# A list item's marker, as typed: "1.", "2)", "iv.", "a)", a bullet or a dash.
_MARKER = re.compile(r"^(?:\d{1,3}[.)]|[ivxIVX]{1,4}[.)]|[a-zA-Z][.)]|[•●▪◦■○◆►✓·*\-–—])$")
_BULLETS = set("•●▪◦■○◆►✓·*-–—")


def _rtl_text(text: str, fallback: bool) -> bool:
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b in _RTL:
            return True
        if b == "L":
            return False
    return fallback


@dataclass
class _Item:
    """A list item's first line: its marker ("3.", "•") and where its text starts."""

    marker: list[Word]  # the marker's words (pieces), typing order
    marker_text: str
    marker_edges: tuple[float, float]  # PDF x
    text_edge: float  # where the text starts: its left edge, or in Arabic its right edge


def _groups(words: list[Word]) -> list[list[Word]]:
    """Words between spaces, in typing order ("3." is "3" and "." joined)."""
    out, current = [], []
    for w in words:
        current.append(w)
        if w.sep:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _marker_kind(text: str) -> str | None:
    """What a line's first word is as a list marker: a number, a letter or Roman
    numeral, a bullet, a dash; or None. In an Arabic line "1." can come as ".1"."""
    for t in (text, text[::-1]):
        if re.fullmatch(r"\d{1,3}[.)]", t):
            return "number"
        if re.fullmatch(r"[a-zA-Z][.)]|[ivxIVX]{1,4}[.)]", t):
            return "letter"
    if text in "•●▪◦■○◆►✓" and len(text) == 1:
        return "bullet"
    if text in ("-", "–", "—", "*", "·"):
        return "dash"
    return None


def _candidate(line: Line, rp: Replay) -> tuple[_Item, str, float] | None:
    """The line read as a list item, without asking whether it really is one: the item,
    its marker's kind, and the gap between marker and text."""
    old = _old_words(line, rp)
    groups: dict[int, list[Word]] = {}
    for w in old:
        groups.setdefault(w.group, []).append(w)
    ordered = sorted(groups.values(), key=lambda g: min(w.u0 for w in g), reverse=line.rtl)
    if len(ordered) < 2:
        return None
    marker, after = ordered[0], ordered[1]
    m0, m1 = min(w.u0 for w in marker), max(w.u1 for w in marker)
    a0, a1 = min(w.u0 for w in after), max(w.u1 for w in after)
    tab = (a0 - m1) if not line.rtl else (m0 - a1)
    text = "".join(w.text for w in marker)
    kind = _marker_kind(text)
    if kind is None or tab < 0.1 * line.style.size:
        return None
    return _Item(marker, text, (m0, m1), a0 if not line.rtl else a1), kind, tab


def _item_of(line: Line, rp: Replay, lines: list[Line] | None = None) -> _Item | None:
    """The line as a list item, if the page shows it is one. "J. Smith" in a signature
    or a heading "1. Scope" starts like an item but isn't one, and text added under it
    must not be indented. Evidence: a bullet; a number a tab away from its text (Word);
    another item of the same kind lined up with it nearby; or the item's next lines
    starting where its text starts."""
    found = _candidate(line, rp)
    if found is None:
        return None
    item, kind, tab = found
    if kind == "bullet" or (kind == "number" and tab >= 0.4 * line.style.size):
        return item
    if lines is None:
        return None
    edge = item.marker_edges[0] if not line.rtl else item.marker_edges[1]
    height = line.written[3] - line.written[1]
    for other in lines:
        if other is line or abs(other.written[1] - line.written[1]) > 8 * height:
            continue
        theirs = _candidate(other, rp)
        if theirs is None or theirs[1] != kind:
            continue
        their_edge = theirs[0].marker_edges[0] if not other.rtl else theirs[0].marker_edges[1]
        if abs(their_edge - edge) < 1.5:
            return item
    below = _lines_below(line, lines)
    if below:
        start = min(w.u0 for w in _old_words(below[0], rp)) if not line.rtl else max(w.u1 for w in _old_words(below[0], rp))
        if abs(start - item.text_edge) < 1.5:
            return item
    return None


def add_line(
    src: bytes, page_no: int, text: str, ref_id: str, ref_text: str, where: str = "below", at: tuple[float, float] | None = None
) -> EditResult:
    """Add `text` as a new line below (or above) the line `ref_id`, in its style. With
    `at`, a point on the page as displayed (in points), the text goes there instead: it
    starts at the point (English runs right from it, Arabic left) with its middle at the
    point's height, lined up with text already on that row or edge if it's close."""
    text = re.sub(r"\s+", " ", clean_text(text)).strip()
    if not text:
        raise EditRejected("Type the text to add.")
    if at is None and where not in ("below", "above"):
        raise EditRejected("A new line goes below or above a line.")
    if "�" in text:
        raise EditRejected("The text has a character that can't be added (�).")
    mu = pymupdf.open("pdf", src)
    if not 0 <= page_no < mu.page_count:
        raise EditRejected("That page doesn't exist.")
    page = mu[page_no]
    all_lines = page_lines(page)
    ref = next((l for l in all_lines if l.id == ref_id), None)
    if ref is None or comparable(ref.text) != comparable(ref_text):
        raise EditRejected("This line changed since you selected it. Select it again.")

    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported as e:
        raise EditRejected(str(e))
    if not matches_trace(rp, page) or rp.stray_restore or rp.open_text:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")

    word_made = made_by_word(mu)
    old = _old_words(ref, rp)
    source = _source_word(ref, old, rp)
    src_glyph = rp.glyphs[source.traces[0]]
    paint = src_glyph.paint
    a, b, c, d = paint.trm2
    if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
        raise EditRejected("Rotated or slanted text can't be used as the style of a new line yet.")
    style = _style_of(ref, source)
    base = src_glyph.origin[1]

    # The new words. In a list, the item the reference line belongs to (its first line has
    # the number), and whether the new text starts with a number or bullet of its own.
    rtl = _rtl_text(text, ref.rtl)
    words = [Word(t, _script(t), source=source, sep=s) for t, s in _split_words(text)]
    for k, w in enumerate(words):
        w.logical = k
    item_line = None if at is not None else next((l for l in [ref, *_lines_above(ref, all_lines)] if _item_of(l, rp, all_lines) is not None), None)
    item = _item_of(item_line, rp, all_lines) if item_line is not None else None
    first = _groups(words)[0]
    new_marker = first if item is not None and rtl == ref.rtl and _MARKER.match("".join(w.text for w in first)) else []
    expected = text
    if new_marker:
        typed = "".join(w.text for w in new_marker)
        for w in new_marker:
            w.source = item.marker[0]  # the list's own number font, size and color
        if typed in _BULLETS and len(item.marker_text) == 1:
            # A bullet is copied as drawn (often a Symbol font character that can't be typed).
            clone = Word(item.marker_text, "N", source=item.marker[0], sep=new_marker[-1].sep)
            clone.clone = item.marker
            words = [clone] + [w for w in words if w not in new_marker]
            for k, w in enumerate(words):
                w.logical = k
            new_marker = [clone]
            expected = item.marker_text + text[len(typed):]
    if word_made:
        visual = _visual_order(words, rtl)
    else:
        words, visual = _visual_order_standard(words, rtl)

    fonts = Fonts(pdf, page_no, rp, word_made, ref)
    ratios: dict[str, float] = {}
    for w in visual:
        if getattr(w, "clone", None):
            w.width = max(x.u1 for x in w.clone) - min(x.u0 for x in w.clone)
            continue
        g = rp.glyphs[w.source.traces[0]]
        w_style = _style_of(ref if w.source in old else item_line, w.source)
        fonts.shape(w, g.paint, w_style)
        if w.choice.full is not None:
            if w_style not in ratios:
                ratios[w_style] = _tracking(ref, old, rp, w_style, w.choice.full, word_made) if w.source in old else 1.0
            r = ratios[w_style]
            w.shaped = [(gid, cl, adv * r, dx * r, dy) for gid, cl, adv, dx, dy in w.shaped]
        w.width = sum(s[2] for s in w.shaped) * g.paint.size * g.paint.Tz * g.paint.trm2[0] / 1000

    space = _space_width(rp, ref)
    inside = _bracket_insides(words, visual)
    gaps, joined = [], []
    for k, (p, q) in enumerate(zip(visual, visual[1:])):
        lead = words[min(p.logical, q.logical)]
        is_joined = (abs(p.logical - q.logical) == 1 and lead.sep == "") or (k, k + 1) in inside
        gaps.append(0.0 if is_joined else space)
        joined.append(is_joined)

    if at is not None:
        return _add_at(src, pdf, page, page_no, rp, ref, all_lines, at, visual, gaps, joined, base, rtl, expected, fonts)

    # Where it goes: one line spacing from the reference, aligned as its paragraph is.
    spacing = _line_spacing(ref, all_lines, rp, style, paint, word_made)
    baseline = base - spacing if where == "below" else base + spacing
    column = _column(ref, old, page)
    neighbours = _paragraph_edges(ref, all_lines, page)
    u0, u1 = min(w.u0 for w in old), max(w.u1 for w in old)
    # The start edge is the reference line's own, unless it's a paragraph's first line
    # and the lines under it start further out (a first-line indent).
    under = [_pdf_rect(l.bbox, page) for l in _lines_below(ref, all_lines)]
    left = min([u0] + [r.x0 for r in under])
    right = max([u1] + [r.x1 for r in under])
    mode = _mode(ref, old, column, space, neighbours, page, all_lines)
    if new_marker:
        # A new item: its number where the item's number is, its text at the item's tab.
        k = len(new_marker) - 1 if not rtl else len(visual) - len(new_marker) - 1
        marker_width = sum(w.width for w in new_marker)
        if not rtl:
            gaps[k] = max(space, item.text_edge - (item.marker_edges[0] + marker_width))
        else:
            gaps[k] = max(space, (item.marker_edges[1] - marker_width) - item.text_edge)
    natural = sum(w.width for w in visual) + sum(gaps)
    if new_marker:
        start = item.marker_edges[0] if not rtl else item.marker_edges[1] - natural
    elif item is not None and rtl == ref.rtl and mode == "start":
        # More text under a list item lines up with the item's text.
        start = item.text_edge if not rtl else item.text_edge - natural
    elif mode == "center":
        start = (u0 + u1) / 2 - natural / 2
    elif mode == "end":
        # Flush to the far edge, as the paragraph is (a right-aligned English line).
        start = u1 - natural if not ref.rtl else u0
    elif rtl:
        # Arabic under an English line starts at the column's right edge.
        start = (right if ref.rtl else column[1]) - natural
    else:
        start = left if not ref.rtl else column[0]
    x = start
    for w, gap in zip(visual, gaps + [0.0]):
        w.x = x
        x += w.width + gap
    x0, x1 = visual[0].x, visual[-1].x + visual[-1].width

    room = _room(ref, all_lines, page, column, u0, u1)
    if x0 < room[0] - 0.01 or x1 > room[1] + 0.01:
        raise EditRejected("The new text is too long for one line. Shorten it, or add it as two lines.")
    ref_box = _pdf_rect(ref.bbox, page)
    new_box = pymupdf.Rect(x0, baseline - (base - ref_box.y0), x1, baseline + (ref_box.y1 - base))
    _check_room(new_box, ref_box, ref, page, all_lines, rp, pdf, page_no, f"{where} this one")
    return _draw(src, pdf, page_no, rp, ref, all_lines, visual, joined, base, baseline, new_box, expected, fonts)


def _add_at(src, pdf, page, page_no, rp, ref, all_lines, at, visual, gaps, joined, base, rtl, expected, fonts) -> EditResult:
    """Place the new text at a point the user tapped."""
    tap = pymupdf.Point(at) * (page.derotation_matrix * ~page_matrix(page))
    ref_box = _pdf_rect(ref.bbox, page)
    height = ref_box.y1 - ref_box.y0
    # Its middle at the tap's height; on the row of text already there, if it's close
    # (an empty table cell next to a filled one).
    baseline = tap.y + (base - (ref_box.y0 + ref_box.y1) / 2)
    row = [l for l in all_lines if _pdf_rect(l.bbox, page).y0 - 0.25 * height <= tap.y <= _pdf_rect(l.bbox, page).y1 + 0.25 * height]
    if row:
        nearest = min(row, key=lambda l: abs((_pdf_rect(l.bbox, page).x0 + _pdf_rect(l.bbox, page).x1) / 2 - tap.x))
        baseline = _baseline(nearest, rp)
    natural = sum(w.width for w in visual) + sum(gaps)
    # Its start at the tap; on the edge of text just above or below, if it's close.
    start_x = tap.x
    nearby = [
        _pdf_rect(l.bbox, page) for l in all_lines
        if abs((_pdf_rect(l.bbox, page).y0 + _pdf_rect(l.bbox, page).y1) / 2 - baseline) < 4 * height
    ]
    edges = [r.x1 for r in nearby] if rtl else [r.x0 for r in nearby]
    close = [e for e in edges if abs(e - tap.x) < 8]
    if close:
        start_x = min(close, key=lambda e: abs(e - tap.x))
    start = start_x if not rtl else start_x - natural
    x = start
    for w, gap in zip(visual, gaps + [0.0]):
        w.x = x
        x += w.width + gap
    x0, x1 = visual[0].x, visual[-1].x + visual[-1].width
    # Within the page's text area (its margins), unless the tap was already outside it.
    left, right = _text_area(page, all_lines)
    paper = _pdf_rect(page.rect, page)
    limit_left = left if start_x > left + 1 else paper.x0 + 10
    limit_right = right if start_x < right - 1 else paper.x1 - 10
    if x0 < limit_left - 0.01 or x1 > limit_right + 0.01:
        raise EditRejected("The new text is too long to fit there. Shorten it, or start it further " + ("right." if rtl else "left."))
    new_box = pymupdf.Rect(x0, baseline - (base - ref_box.y0), x1, baseline + (ref_box.y1 - base))
    _check_room(new_box, None, ref, page, all_lines, rp, pdf, page_no, "there")
    return _draw(src, pdf, page_no, rp, ref, all_lines, visual, joined, base, baseline, new_box, expected, fonts)


def _text_area(page: pymupdf.Page, lines: list[Line]) -> tuple[float, float]:
    """The page's text area (PDF x): its text's outer edges, widened so the side margins
    mirror each other, as in most documents."""
    paper = _pdf_rect(page.rect, page)
    boxes = [_pdf_rect(l.bbox, page) for l in lines]
    if not boxes:
        return paper.x0 + 36, paper.x1 - 36
    left, right = min(r.x0 for r in boxes), max(r.x1 for r in boxes)
    return min(left, paper.x0 + (paper.x1 - right)), max(right, paper.x1 - (left - paper.x0))


def _draw(src, pdf, page_no, rp, ref, all_lines, visual, joined, base, baseline, new_box, expected, fonts) -> EditResult:
    """Draw the new words (and the reference line's own space glyph in the gaps), then check."""
    runs: list[PlacedRun] = []
    embed_cache: dict = {}
    new_fonts: dict = {}
    for w in visual:
        if not getattr(w, "clone", None) and w.choice.resource is None:
            used = new_fonts.setdefault(w.choice.full.entry, {})
            for (gid, *_), t in zip(w.shaped, _glyph_texts(w.text, w.shaped, w.choice.full)):
                used.setdefault(gid, t)
    for entry, used in new_fonts.items():
        fonts.resource_for(fontlib.load_full_font(entry), used, embed_cache)
    for w in visual:
        if getattr(w, "clone", None):
            dx = w.x - min(x.u0 for x in w.clone)
            for t in (t for x in w.clone for t in x.traces):
                g = rp.glyphs[t]
                place = (g.origin[0] + dx, baseline + (g.origin[1] - base))
                runs.append(PlacedRun(g.paint.font, rp.fonts[g.paint.font], g.paint, [PlacedGlyph(g.code, place)]))
            continue
        p = rp.glyphs[w.source.traces[0]]
        wp = p.paint
        resource = w.choice.resource or embed_cache[w.choice.full.entry]
        info = w.choice.font_info or FontInfo(resource, pdf.pages[page_no].obj.Resources.Font[resource])
        draw_paint = Paint(resource, wp.size, wp.Tz, wp.Tr, wp.trm2, wp.fill, wp.stroke, wp.extgstates)
        em_x = wp.size * wp.Tz * wp.trm2[0] / 1000
        em_y = wp.size * wp.trm2[3] / 1000
        rise = p.origin[1] - base  # a list number may sit a little off the text's baseline
        pen, placed = 0.0, []
        for (gid, cl, adv, dx, dy), code in zip(w.shaped, w.choice.codes):
            placed.append(PlacedGlyph(code, (w.x + (pen + dx) * em_x, baseline + rise + dy * em_y)))
            pen += adv
        runs.append(PlacedRun(resource, info, draw_paint, placed))
    spaces = [t for g in ref.glyphs if g.is_space and not g.synthetic for t in g.traces]
    if spaces:
        sp = rp.glyphs[spaces[0]]
        advance = sp.advance / 1000 * sp.paint.size * sp.paint.Tz * sp.paint.trm2[0]
        for k, (p, q) in enumerate(zip(visual, visual[1:])):
            if not joined[k] and q.x - (p.x + p.width) >= 0.6 * advance:
                runs.append(PlacedRun(sp.paint.font, rp.fonts[sp.paint.font], sp.paint, [PlacedGlyph(sp.code, (p.x + p.width, baseline))]))

    # At the very end, with the page's drawing state restored: wrap the page's own
    # instructions in q ... Q (closing any it left open) and draw in page coordinates.
    # The page's own streams are kept byte for byte.
    page_obj = pdf.pages[page_no].obj
    original = page_obj.Contents
    streams = list(original) if isinstance(original, pikepdf.Array) else [original]
    closing = [pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q"))] * (rp.open_states + 1)
    tail = pikepdf.unparse_content_stream(closing + draw_block(runs, IDENTITY, None))
    page_obj.Contents = pikepdf.Array([pdf.make_stream(b"q\n"), *streams, pdf.make_stream(b"\n" + tail)])
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()

    added = sum(len(r.glyphs) for r in runs)
    area, mark = _verify(src, result, page_no, rp, all_lines, expected, new_box, added)
    return EditResult(result, list(dict.fromkeys(fonts.warnings)), expected, [(page_no, *area)], [(page_no, *mark)])


def _lines_below(ref: Line, lines: list[Line]) -> list[Line]:
    """The next lines of the reference line's paragraph (as the page was written)."""
    out, current = [], ref
    while True:
        x0, y0, x1, y1 = current.written
        h = y1 - y0
        nxt = [
            m for m in lines
            if m is not current and m not in out and m is not ref
            and 0 <= m.written[1] - y1 < 0.8 * h and min(m.written[2], x1) - max(m.written[0], x0) > 0
            and abs(m.style.size - current.style.size) < 0.5
        ]
        if not nxt:
            return out
        current = min(nxt, key=lambda m: m.written[1])
        out.append(current)


def _lines_above(ref: Line, lines: list[Line]) -> list[Line]:
    """The earlier lines of the reference line's paragraph, nearest first."""
    out, current = [], ref
    while True:
        x0, y0, x1, y1 = current.written
        h = y1 - y0
        prev = [
            m for m in lines
            if m is not current and m not in out and m is not ref
            and 0 <= y0 - m.written[3] < 0.8 * h and min(m.written[2], x1) - max(m.written[0], x0) > 0
            and abs(m.style.size - current.style.size) < 0.5
        ]
        if not prev:
            return out
        current = max(prev, key=lambda m: m.written[3])
        out.append(current)


def _source_word(ref: Line, old: list[Word], rp: Replay) -> Word:
    """The word whose look the new line takes: one in the line's main font, visible,
    with one look throughout, and not a list's number or bullet."""
    def visible(w):
        return rp.glyphs[w.traces[0]].paint.Tr not in (3, 7)

    candidates = [w for w in old if w.text.strip() and visible(w)]
    if not candidates:
        raise EditRejected("This line is invisible text (as in a scanned page), so its style can't be copied.")
    found = _candidate(ref, rp)
    item = found[0] if found is not None else None  # a marker isn't a style to copy, list or not
    if item is not None:
        candidates = [w for w in candidates if w not in item.marker] or candidates
    for w in candidates:
        if _style_of(ref, w) == ref.style.font and len(_looks(w, ref, rp)[1]) == 1:
            return w
    return candidates[0]


def _column(ref: Line, old: list[Word], page: pymupdf.Page) -> tuple[float, float]:
    if ref.column:
        r = pymupdf.Rect(ref.column[0], ref.written[1], ref.column[1], ref.written[3]) * ~page_matrix(page)
        r.normalize()
        return (r.x0, r.x1)
    return (min(w.u0 for w in old), max(w.u1 for w in old))


def _mode(ref: Line, old: list[Word], column, space: float, neighbours, page: pymupdf.Page, lines: list[Line]) -> str:
    """center, end (flush to the edge a line doesn't start from: a right-aligned English
    line) or start. A justified paragraph's last line is start-aligned, and so is a new one."""
    mode = _alignment(ref, sorted(old, key=lambda w: w.u0), column, space, neighbours)
    if mode == "center":
        return "center"
    if mode == "justify":
        return "start"
    u0, u1 = min(w.u0 for w in old), max(w.u1 for w in old)
    # The far edge: the column's, or the page's margin mirrored from the near one (marks
    # outside the margin, like Word's Arabic list numbers, push the column's edge out).
    paper = _pdf_rect(page.rect, page)
    lefts = [_pdf_rect(l.bbox, page).x0 for l in lines]
    rights = [_pdf_rect(l.bbox, page).x1 for l in lines]
    if not ref.rtl:
        edges, near = (column[1], paper.x1 - (min(lefts) - paper.x0)), u0 - column[0]
        far = min(abs(e - u1) for e in edges)
    else:
        edges, near = (column[0], paper.x0 + (paper.x1 - max(rights))), column[1] - u1
        far = min(abs(u0 - e) for e in edges)
    if far < 2.5 + space and near > 3 * space:
        return "end"
    return "start"


def _baseline(line: Line, rp: Replay) -> float:
    """The baseline most of the line's letters sit on (not a superscript, not an invisible
    space Word draws for an empty paragraph nearby)."""
    ys = Counter(round(rp.glyphs[g.traces[0]].origin[1], 2) for g in line.glyphs if g.traces and not g.is_space)
    if ys:
        return ys.most_common(1)[0][0]
    return rp.glyphs[line.members[0]].origin[1]


def _line_spacing(ref: Line, lines: list[Line], rp: Replay, style: str, paint: Paint, word_made: bool) -> float:
    """Baseline to baseline. Measured in the reference line's paragraph when it has more
    than one line; else the most common spacing of lines in the same font and size on
    the page; else the font's own line height (what Word uses for single spacing)."""
    size = ref.style.size

    def same_style(l):
        return l.style.font == ref.style.font and abs(l.style.size - size) < 0.1

    def next_below(l):
        x0, y0, x1, y1 = l.written
        h = y1 - y0
        below = [
            m for m in lines
            if m is not l and same_style(m) and 0 <= m.written[1] - y1 < 0.8 * h
            and min(m.written[2], x1) - max(m.written[0], x0) > 0
        ]
        return min(below, key=lambda m: m.written[1]) if below else None

    gaps = []
    for l in (ref, *[m for m in lines if m is not ref and same_style(m)]):
        n = next_below(l)
        if n is not None:
            gaps.append((l is ref or n is ref, round(_baseline(l, rp) - _baseline(n, rp), 2)))
    own = [g for mine, g in gaps if mine]
    if own:
        return own[0]
    common = Counter(g for _, g in gaps).most_common(1)
    if common:
        return common[0][0]
    entry = fontlib.library().find(style)
    if entry is not None:
        full = fontlib.load_full_font(entry)
        hhea = full.tt["hhea"]
        height = (hhea.ascent - hhea.descent + hhea.lineGap) / full.upem * paint.size * paint.trm2[3]
        # Word lays lines out in steps of 0.12 pt.
        return round(height / 0.12) * 0.12 if word_made else height
    return 1.2 * paint.size * paint.trm2[3]


def _check_room(box: pymupdf.Rect, ref_box: pymupdf.Rect | None, ref: Line, page: pymupdf.Page, lines: list[Line], rp: Replay, pdf, page_no: int, where: str) -> None:
    """Refuse a place where the new line would touch anything: other text, a picture, a
    form field, a line of a table or the edge of the page; or leave its table cell."""
    no_room = f"There's no room for a new line {where}"
    shown = pymupdf.Rect(to_display(page, tuple(box)))
    if not (page.rect + (-0.5, -0.5, 0.5, 0.5)).contains(shown):
        raise EditRejected(f"{no_room}: it would go past the edge of the page.")
    tight = box + (0.25, 0.25, -0.25, -0.25)
    rules = decorations._vertical_rules(rp)  # table borders, as (x, y0, y1) in PDF space
    height = box.y1 - box.y0
    for l in lines:
        r = _pdf_rect(l.bbox, page)
        if any(unicodedata.combining(ch) for ch in l.text):
            # Vowel marks reach above and below the font's height.
            r += (0, -0.3 * l.style.size, 0, 0.35 * l.style.size)
        if r.intersects(tight):
            hint = (
                "Tap an empty spot." if ref_box is None
                else "Text doesn't move down to make room, so add it after the last line of a paragraph."
            )
            raise EditRejected(f"{no_room}: it would overlap other text. {hint}")
        overlap = min(r.y1, box.y1) - max(r.y0, box.y0)
        if overlap <= 0.3 * height:
            continue
        # A table's border between them keeps them apart: cells never read as one line.
        gap0, gap1 = (r.x1, box.x0) if r.x1 <= box.x0 else (box.x1, r.x0)
        if any(gap0 - 1 <= x <= gap1 + 1 and y0 < max(r.y1, box.y1) and y1 > min(r.y0, box.y0) for x, y0, y1 in rules):
            continue
        distance = max(r.x0 - box.x1, box.x0 - r.x1)
        # Half a row off a line nearby: crowded, and its vowel marks would mix with ours.
        if abs((r.y0 + r.y1) / 2 - (box.y0 + box.y1) / 2) > 0.15 * height and distance < 8 * l.style.size:
            raise EditRejected(f"{no_room}: it would be crowded against other text.")
        # On the same row, close by (another column's text): it would read as one line.
        if distance < 4 * l.style.size:
            raise EditRejected(f"{no_room}: it would sit right next to other text on the same row.")
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) * ~page_matrix(page)  # images come unrotated, like text
        r.normalize()
        if r.intersects(tight) and not (r.contains(box) and r.width * r.height > 4 * box.width * box.height):
            raise EditRejected(f"{no_room}: it would overlap a picture.")
    for a in pdf.pages[page_no].obj.get("/Annots") or []:
        if a.get("/Subtype") == pikepdf.Name.Widget and "/Rect" in a:
            r = pymupdf.Rect(*(float(v) for v in a.Rect))
            r.normalize()
            if r.intersects(tight):
                raise EditRejected(f"{no_room}: a form field is there.")
    # Between the reference line and the new one: the gap the text moves across.
    between = (
        pymupdf.Rect(min(box.x0, ref_box.x0), min(box.y1, ref_box.y1), max(box.x1, ref_box.x1), max(box.y0, ref_box.y0))
        if ref_box is not None
        else pymupdf.Rect()
    )
    own = {d.path for d in decorations.for_line(ref, rp)}
    for index, p in enumerate(rp.paths):
        if p.clip or not (p.fills or p.strokes) or index in own:
            continue
        for s in p.subpaths:
            r = pymupdf.Rect(s.bbox)
            if p.strokes:
                r += (-p.line_width / 2, -p.line_width / 2, p.line_width / 2, p.line_width / 2)
            if ref_box is not None and r.height <= 2.5 and r.width > 4 and r.intersects(between) and between.y0 <= (r.y0 + r.y1) / 2 <= between.y1:
                raise EditRejected(f"{no_room}: it would cross a border, out of its table cell or paragraph.")
            if not r.intersects(tight):
                continue
            if p.fills and not p.strokes and r.contains(box):
                continue  # a background: page color, a cell's shading
            raise EditRejected(f"{no_room}: it would cross a line or a shape on the page.")


def _verify(old_bytes: bytes, new_bytes: bytes, page_no: int, rp: Replay, lines: list[Line], text: str, box: pymupdf.Rect, added: int) -> tuple:
    """Check the addition; return the area (as displayed) outside which nothing changed,
    and the new line's box, to show people."""
    old_doc, new_doc = pymupdf.open("pdf", old_bytes), pymupdf.open("pdf", new_bytes)
    old_page, page = old_doc[page_no], new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new_bytes)), page_no)
    except Unsupported:
        fail("the edited page can't be read back")
    if not matches_trace(new_rp, page):
        fail("the edited page can't be read back")
    # Every glyph already on the page is drawn exactly as before; the new ones follow.
    if len(new_rp.glyphs) != len(rp.glyphs) + added:
        fail("other text on the page would have changed")
    for a, b in zip(rp.glyphs, new_rp.glyphs):
        if (
            a.code != b.code
            or abs(a.origin[0] - b.origin[0]) > 0.01
            or abs(a.origin[1] - b.origin[1]) > 0.01
            or (a.paint.font, a.paint.size, a.paint.fill) != (b.paint.font, b.paint.size, b.paint.fill)
        ):
            fail("other text on the page would have changed")

    # Nothing outside the new line changes, in two engines, and the new line shows.
    area = to_display(page, tuple(box)) + (-3, -3, 3, 3)
    old_mu, old_pf = render(old_bytes, page_no)
    new_mu, new_pf = render(new_bytes, page_no)
    if outside(old_mu, new_mu, [tuple(area)], 2.0, 0) or outside(old_pf, new_pf, [tuple(area)], 2.0, 1):
        fail("other parts of the page would have changed")
    for before, after in ((old_mu, new_mu), (old_pf, new_pf)):
        if ink(after, tuple(area), 2.0) <= ink(before, tuple(area), 2.0):
            fail("the new text wasn't visible")

    # It reads back as typed, as a line of its own; the other lines read as before.
    new_lines = page_lines(page)
    mine = [l for l in new_lines if pymupdf.Rect(l.bbox).intersects(area) and comparable(l.text) == comparable(text)]
    if len(mine) != 1:
        got = " | ".join(l.text for l in new_lines if pymupdf.Rect(l.bbox).intersects(area))
        fail(f"it read back as “{got}”")
    others = sorted(comparable(l.text) for l in new_lines if l is not mine[0])
    if others != sorted(comparable(l.text) for l in lines):
        fail("the lines around it would read differently")
    # PDFium finds its letters there too.
    doc = pdfium.PdfDocument(new_bytes)
    try:
        tp = doc[page_no].get_textpage()
        pr = pymupdf.Rect(area) * page.derotation_matrix * ~page_matrix(page)
        pr.normalize()
        found = tp.get_text_bounded(left=pr.x0, bottom=pr.y0, right=pr.x1, top=pr.y1)
    finally:
        doc.close()
    letters = lambda t: {ch for ch in unicodedata.normalize("NFKC", clean_text(t)) if ch.isalnum()}
    if not letters(text) <= letters(found):
        fail("a second PDF reader saw different letters")

    check_drawings(old_page, page, [], rp)
    check_links(old_page, page, [])
    check_other_pages(old_bytes, new_bytes, {page_no})
    return tuple(area), tuple(mine[0].bbox)
