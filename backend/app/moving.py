"""Move the text below a spot down, to make room for a new line, or up, into the space a
deleted line leaves, as a word processor would.

What moves: everything drawn in the spot's column (the page's width, or a table cell
between its borders) below the spot, down to the first thing that has to stay: a footer
or page number, a table border, anything only partly in the column. Each moved text
object, shape and picture keeps its own instructions; a translation (cm) is put before it
and undone right after it, so drawing order, clipping and later edits stay as they were.

Refused, with the reason, when it can't be done exactly: something drawn across the spot
(a box around the text), text drawn together with the text above it, a column beside
other text, a color gradient, a form field, or no room before the bottom of the page or
the cell. Nothing moves onto another page.

Checked before it's accepted: every glyph, shape, picture and link is where it was or
exactly as far lower (or higher); nothing outside the moved block's old and new places
changes by a pixel (MuPDF and PDFium); the block looks the same, pixel for pixel, at its
new place; and every line reads the same.
"""

import io
import math
import re
from dataclasses import dataclass, field

import numpy as np
import pikepdf
import pymupdf

from . import decorations
from .changes import (
    EditRejected,
    LinkChange,
    _drawings,
    apply_links,
    check_links,
    check_other_pages,
    fail,
    outside,
    page_links,
    render,
    to_display,
    to_pdf,
    write_page,
)
from .pdfcontent import Matrix, Replay, Unsupported, inverse, matches_trace, mul, replay
from .textlayout import Line, page_lines


class MoveRefused(Exception):
    """The text can't be moved exactly. The message says why, to follow "because" or ":"."""


_DIGITS = re.compile(r"[0-9٠-٩۰-۹]+")
Box = tuple[float, float, float, float]  # PDF space: x0, y0 (bottom), x1, y1 (top)


@dataclass
class Piece:
    """Something drawn that moves as a whole: a text object (BT…ET), a path, a picture."""

    kind: str  # "text", "path", "clip" (a clipping path that paints nothing), "image"
    start: int  # its first instruction
    end: int  # its last instruction
    ctm: Matrix  # in effect at its start (the same throughout)
    box: Box
    glyphs: tuple[int, ...] = ()
    index: int = -1  # into Replay.paths or Replay.images
    background: bool = False  # filled with no outline: a page's or a cell's color


@dataclass
class Shift:
    page_no: int
    dy: float  # PDF units; positive moves down (y decreases)
    pieces: list[Piece]
    links: list[LinkChange]
    old_box: Box  # everything that moves, before
    lines: list[str] = field(default_factory=list)  # ids of the lines that move, as read before

    @property
    def glyphs(self) -> set[int]:
        return {t for p in self.pieces for t in p.glyphs}


def _union(boxes) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _glyph_box(g) -> Box:
    """A glyph's usual ink (PDF space), a little tighter than the font's full height so
    lines at normal spacing don't touch."""
    p = g.paint
    a, _b, _c, d = p.trm2
    w = max(g.advance, 0.0) / 1000 * p.size * p.Tz * a
    em = p.size * d
    x, y = g.origin
    return (min(x, x + w), y - 0.2 * em, max(x, x + w), y + 0.75 * em)


def _pieces(rp: Replay) -> list[Piece]:
    out = []
    by_op: dict[int, list[int]] = {}
    for t, g in enumerate(rp.glyphs):
        by_op.setdefault(g.op, []).append(t)
    start, glyphs, seen = None, [], 0
    for k, ins in enumerate(rp.instructions):
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            continue
        op = str(ins.operator)
        if op == "BT":
            start, glyphs = k, []
        elif op == "ET" and start is not None:
            if glyphs:
                box = _union(_glyph_box(rp.glyphs[t]) for t in glyphs)
                out.append(Piece("text", start, k, rp.glyphs[glyphs[0]].ctm, box, tuple(glyphs)))
                seen += len(glyphs)
            start = None
        elif start is not None and k in by_op:
            glyphs += by_op[k]
    if seen != len(rp.glyphs):
        raise MoveRefused("some text on the page is drawn in an unusual way")
    for i, p in enumerate(rp.paths):
        x0, y0, x1, y1 = p.bbox
        scale = max(abs(p.ctm[0]), abs(p.ctm[1]), abs(p.ctm[2]), abs(p.ctm[3]))
        w = p.line_width * scale / 2 if p.strokes else 0.0
        kind = "path" if (p.fills or p.strokes) else "clip"
        out.append(Piece(kind, p.start, p.end, p.ctm, (x0 - w, y0 - w, x1 + w, y1 + w), index=i, background=p.fills and not p.strokes))
    for i, im in enumerate(rp.images):
        out.append(Piece("image", im.op, im.op, im.ctm, im.bbox, index=i))
    return out


def _clip_scopes(rp: Replay) -> dict[int, int]:
    """For each clipping path (index into rp.paths): the instruction where it stops
    applying, the Q that restores the state it was set in (or the end of the page)."""
    depth, depths = 0, []
    for ins in rp.instructions:
        depths.append(depth)
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            continue
        op = str(ins.operator)
        if op == "q":
            depth += 1
        elif op == "Q":
            depth = max(0, depth - 1)
    out = {}
    for i, p in enumerate(rp.paths):
        if not p.clip:
            continue
        d = depths[p.end]
        end = len(rp.instructions)
        for k in range(p.end + 1, len(rp.instructions)):
            ins = rp.instructions[k]
            if not isinstance(ins, pikepdf.ContentStreamInlineImage) and str(ins.operator) == "Q" and depths[k] == d:
                end = k
                break
        out[i] = end
    return out


def _crop(pdf: pikepdf.Pdf, page_no: int) -> Box:
    box = [float(v) for v in pdf.pages[page_no].cropbox]
    return (min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3]))


def _pdf(page: pymupdf.Page, bbox) -> Box:
    return to_pdf(page, bbox)


def column_of(anchor: Box, rp: Replay, crop: Box) -> tuple[float, float, bool]:
    """The x range the text moves in (PDF): a table cell between its side borders, or the
    page's width. True when it's a cell."""
    mid = (anchor[1] + anchor[3]) / 2
    rules = decorations._vertical_rules(rp)
    lefts = [x for x, a, b in rules if x <= anchor[0] + 1 and a < mid < b]
    rights = [x for x, a, b in rules if x >= anchor[2] - 1 and a < mid < b]
    if lefts and rights:
        return max(lefts), min(rights), True
    return crop[0], crop[2], False


def _beside(anchor: Box, others: list[Box], size: float, rules) -> bool:
    """Text on the anchor's row, apart from it with no table border between: a second column."""
    for r in others:
        overlap = min(r[3], anchor[3]) - max(r[1], anchor[1])
        if overlap < 0.5 * min(r[3] - r[1], anchor[3] - anchor[1]):
            continue
        gap = max(r[0] - anchor[2], anchor[0] - r[2])
        if gap <= size:
            continue
        lo, hi = (anchor[2], r[0]) if r[0] >= anchor[2] else (r[2], anchor[0])
        if any(lo - 1 <= x <= hi + 1 and a < max(r[3], anchor[3]) and b > min(r[1], anchor[1]) for x, a, b in rules):
            continue
        return True
    return False


def _sig(text: str) -> str:
    return _DIGITS.sub("#", " ".join(text.split()))


def bottom_limit(doc: pymupdf.Document, page_no: int, lines: list[Line], crop: Box) -> tuple[float, set[str]]:
    """How low body text may go (PDF y), and the ids of the lines on this page that stay
    put like footers: lines repeated at the same place on the pages around (a footer, a
    page number), and lines wholly inside the bottom margin.

    The bottom margin is taken from the evidence: the lowest body text on the pages
    around; else it's as wide as the narrower side margin, as in most documents."""
    page = doc[page_no]
    height = page.rect.height
    others = []
    for n in (page_no + 1, page_no - 1, page_no + 2, page_no - 2):
        if 0 <= n < doc.page_count and len(others) < 3:
            other = doc[n]
            if abs(other.rect.width - page.rect.width) <= 1 and abs(other.rect.height - height) <= 1:
                others.append((other, page_lines(other)))

    def repeated(line: Line, pool: list[Line]) -> bool:
        s = _sig(line.text)
        return any(
            _sig(o.text) == s and abs(o.bbox[1] - line.bbox[1]) <= 2 and abs(o.bbox[3] - line.bbox[3]) <= 2
            and min(o.bbox[2], line.bbox[2]) - max(o.bbox[0], line.bbox[0]) > -30
            for o in pool
        )

    def footer_zone(line: Line) -> bool:
        return line.bbox[1] > 0.75 * height

    footers = {l.id for l in lines if footer_zone(l) and any(repeated(l, pool) for _, pool in others)}

    boxes = [_pdf(page, l.bbox) for l in lines]
    # Side margins from the body text: not headers and footers, which often reach further out.
    body = [b for l, b in zip(lines, boxes) if l.id not in footers and 0.1 * height < l.bbox[1] and l.bbox[3] < 0.9 * height]
    if body:
        margin = min(min(b[0] for b in body) - crop[0], crop[2] - max(b[2] for b in body))
    else:
        margin = 72.0
    limit = crop[1] + min(max(margin, 18.0), 108.0)
    for other, pool in others:
        rest = [x for _, p in others if p is not pool for x in p] + lines
        body = [o for o in pool if not (footer_zone(o) and repeated(o, rest))]
        if body:
            lowest = max(o.bbox[3] for o in body)
            limit = min(limit, to_pdf(other, (0, lowest, 1, lowest))[1])
    for l, b in zip(lines, boxes):
        if b[3] < limit:
            footers.add(l.id)
    return limit, footers


def plan(pdf: pikepdf.Pdf, doc: pymupdf.Document, page_no: int, rp: Replay, lines: list[Line], anchor: Box, size: float, cut: float, dy: float) -> Shift | None:
    """What moves, and whether it can: everything in the anchor's column below `cut` (PDF
    y), down by dy (up when negative). None when nothing is below."""
    page = doc[page_no]
    if rp.shadings:
        raise MoveRefused("the page has a color gradient, which can't be moved yet")
    crop = _crop(pdf, page_no)
    cx0, cx1, cell = column_of(anchor, rp, crop)
    rules = decorations._vertical_rules(rp)
    rects = {l.id: _pdf(page, l.bbox) for l in lines}
    if not cell:
        others = [r for r in rects.values() if not (r[0] >= anchor[0] - 0.5 and r[2] <= anchor[2] + 0.5 and r[1] >= anchor[1] - 0.5 and r[3] <= anchor[3] + 0.5)]
        if _beside(anchor, others, size, rules):
            raise MoveRefused("this text sits in a column beside other text")
    limit, footer_ids = bottom_limit(doc, page_no, lines, crop)
    footer_glyphs = {t for l in lines if l.id in footer_ids for t in l.members}
    margin = 0.5 * size

    moving: list[Piece] = []
    stoppers: list[Piece] = []
    above: list[Piece] = []
    clips: list[Piece] = []
    pieces = _pieces(rp)
    width = cx1 - cx0

    def across_cell(p: Piece) -> bool:  # a horizontal border line spanning the cell
        return p.kind == "path" and p.box[3] - p.box[1] <= 3 and p.box[0] <= cx0 + 0.25 * width and p.box[2] >= cx1 - 0.25 * width

    # In a table cell, its bottom border is the limit, however close under the text it is;
    # nothing under it belongs to the cell.
    cell_floor = None
    if cell:
        mid = (anchor[1] + anchor[3]) / 2
        floors = [(p.box[1] + p.box[3]) / 2 for p in pieces if across_cell(p) and (p.box[1] + p.box[3]) / 2 < mid]
        cell_floor = max(floors, default=None)
    for p in pieces:
        x0, y0, x1, y1 = p.box
        inside = x0 >= cx0 - 0.5 and x1 <= cx1 + 0.5
        apart = x1 <= cx0 + 0.5 or x0 >= cx1 - 0.5
        if p.kind == "clip":
            clips.append(p)  # decided by what it clips, below
            continue
        if cell_floor is not None and y1 <= cell_floor + 1.5:
            if across_cell(p) and abs((y0 + y1) / 2 - cell_floor) < 0.01:
                stoppers.append(p)
            continue
        if cell and p.kind == "path" and x1 - x0 <= 3 and min(abs((x0 + x1) / 2 - cx0), abs((x0 + x1) / 2 - cx1)) < 1.5:
            continue  # the cell's own side borders
        if p.kind == "text":
            ys = [rp.glyphs[t].origin[1] for t in p.glyphs]
            if min(ys) >= cut:
                above.append(p)
                continue
            if max(ys) >= cut:
                if apart:
                    continue
                raise MoveRefused("some text below is drawn together with the text above it")
        else:
            if y1 > cut + margin and y0 < cut - margin:
                if apart:
                    continue
                if p.background and x0 <= cx0 + 2 and x1 >= cx1 - 2:
                    continue  # a page's or a cell's color: it stays behind
                covers = x0 <= cx0 + 0.1 * width and x1 >= cx1 - 0.1 * width and y1 - y0 >= 0.5 * (crop[3] - crop[1])
                if p.kind == "image" and covers:
                    continue  # a picture behind the whole page (a letterhead): it stays behind
                raise MoveRefused("a box or line is drawn across this spot")
            if (y0 + y1) / 2 >= cut:
                above.append(p)
                continue
        # Below the spot.
        if apart:
            continue
        if cell and across_cell(p):
            stoppers.append(p)  # a border between rows (Word draws one per cell)
            continue
        if not inside or (p.kind == "text" and footer_glyphs.intersection(p.glyphs)) or y1 < limit:
            stoppers.append(p)
        else:
            moving.append(p)

    stop = max((p.box[3] for p in stoppers), default=None)
    if stop is not None:
        across = [p for p in moving if p.box[3] > stop + 0.01 and p.box[1] < stop - 0.01]
        if across:
            raise MoveRefused("text below runs into a line or border that has to stay")
        moving = [p for p in moving if p.box[1] >= stop - 0.01]
    if not moving:
        return None
    # A clipping path moves when everything it clips moves (Chrome cuts the gaps in an
    # underline, under a "g" or "p", with a page-sized clip that has holes in it).
    scopes = _clip_scopes(rp)
    starts = {p.start for p in moving}
    for c in clips:
        end = scopes.get(c.index, len(rp.instructions))
        clipped = [p for p in pieces if p.kind != "clip" and c.end < p.start < end]
        if clipped and all(p.start in starts for p in clipped):
            moving.append(c)
    moving.sort(key=lambda p: p.start)

    for p in moving:
        for t in p.glyphs:
            g = rp.glyphs[t]
            a, b, c, d = g.paint.trm2
            if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
                raise MoveRefused("some text below is turned or mirrored")
            if g.paint.Tr >= 4:
                raise MoveRefused("some text below is used to clip other things")

    drawn = [p for p in moving if p.kind != "clip"]
    box = _union(p.box for p in drawn)
    if dy > 0:
        floor = limit
        what = "the text below would run past the bottom of the page"
        if stop is not None and stop + 0.3 * size > floor:
            floor = stop + 0.3 * size
            blocker = max(stoppers, key=lambda p: p.box[3])
            if cell and blocker.kind == "path":
                what = "the table cell is full (cells don't grow yet)"
            elif blocker.kind == "text":
                what = "the text below would run into the page's footer"
            else:
                what = "the text below would run into a line or border below it"
        if box[1] - dy < floor - 0.01:
            raise MoveRefused(what)
    else:
        visible = {t for l in lines for t in l.members}
        for p in drawn:
            if p.kind == "text" and not visible.intersection(p.glyphs):
                continue  # invisible marks (Word's empty paragraphs)
            moved = (p.box[0], p.box[1] - dy, p.box[2], p.box[3] - dy)
            for f in above:
                if f.kind == "clip" or f.background:
                    continue
                if f.kind == "text" and not visible.intersection(f.glyphs):
                    continue
                if f.box[2] <= cx0 + 0.5 or f.box[0] >= cx1 - 0.5:
                    continue
                if min(moved[2], f.box[2]) - max(moved[0], f.box[0]) > 0.01 and min(moved[3], f.box[3]) - max(moved[1], f.box[1]) > 0.01:
                    raise MoveRefused("the text below would overlap the text above")

    # Links move with their text; form fields and comments in the way can't.
    moved_glyphs = {t for p in moving for t in p.glyphs}
    region = (cx0, min(box[1], box[1] - dy), cx1, max(cut, box[3] - dy))
    links = []
    for link in page_links(pdf, page_no, rp):
        under = link.glyphs & moved_glyphs
        if link.glyphs and under and under != link.glyphs:
            raise MoveRefused("a link covers text both above and below this spot")
        x0, y0, x1, y1 = link.rect
        centre_in = cx0 <= (x0 + x1) / 2 <= cx1 and box[1] - 0.5 <= (y0 + y1) / 2 <= min(cut, box[3] + 0.5)
        if under or (not link.glyphs and centre_in):
            links.append(LinkChange(link, (x0, y0 - dy, x1, y1 - dy)))
    for a in pdf.pages[page_no].obj.get("/Annots") or []:
        subtype = a.get("/Subtype")
        if subtype in (pikepdf.Name.Link, pikepdf.Name.Popup) or "/Rect" not in a:
            continue
        r = [float(v) for v in a.Rect]
        r = (min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3]))
        if min(r[2], region[2]) > max(r[0], region[0]) and min(r[3], region[3]) > max(r[1], region[1]):
            raise MoveRefused("a form field is in the way" if subtype == pikepdf.Name.Widget else "a comment or stamp is in the way")

    moved_lines = []
    for l in lines:
        members = set(l.members)
        if members & moved_glyphs:
            if not members <= moved_glyphs:
                raise MoveRefused("a line below would be split")
            moved_lines.append(l.id)
    if not moved_lines and not any(p.kind in ("path", "image") for p in moving):
        return None  # only invisible marks (Word's empty paragraphs): nothing to make room for
    return Shift(page_no, dy, moving, links, box, moved_lines)


def _cm(p: Piece, dy: float) -> tuple[list, list]:
    """A cm moving what follows by dy on the page (down when positive), and its inverse."""
    m = mul(mul(p.ctm, (1, 0, 0, 1, 0, -dy)), inverse(p.ctm))
    tx, ty = round(m[4], 6), round(m[5], 6)
    return [1, 0, 0, 1, tx, ty], [1, 0, 0, 1, -tx, -ty]


def apply(pdf: pikepdf.Pdf, page_no: int, rp: Replay, shift: Shift) -> None:
    before: dict[int, list] = {}
    after: dict[int, list] = {}
    for p in shift.pieces:
        go, back = _cm(p, shift.dy)
        before.setdefault(p.start, []).append(pikepdf.ContentStreamInstruction(go, pikepdf.Operator("cm")))
        after.setdefault(p.end, []).insert(0, pikepdf.ContentStreamInstruction(back, pikepdf.Operator("cm")))
    out = []
    for k, ins in enumerate(rp.instructions):
        out += before.get(k, [])
        out.append(ins)
        out += after.get(k, [])
    write_page(pdf, page_no, out)
    apply_links(pdf, page_no, shift.links)


def _close(a, b, tolerance: float) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def verify(old_bytes: bytes, new_bytes: bytes, page_no: int, rp: Replay, shift: Shift, lines: list[Line]) -> list[tuple]:
    """Check the move; return the areas (page, as displayed) outside which nothing changed."""
    old_doc, new_doc = pymupdf.open("pdf", old_bytes), pymupdf.open("pdf", new_bytes)
    old_page, new_page = old_doc[page_no], new_doc[page_no]
    new_pdf = pikepdf.open(io.BytesIO(new_bytes))
    try:
        new_rp = replay(new_pdf, page_no)
    except Unsupported:
        fail("the page can't be read back")
    if not matches_trace(new_rp, new_page):
        fail("the page can't be read back")
    dy = shift.dy

    # Every glyph is drawn as before: where it was, or exactly dy lower.
    moved = shift.glyphs
    if len(new_rp.glyphs) != len(rp.glyphs):
        fail("text on the page would have changed")
    for t, (a, b) in enumerate(zip(rp.glyphs, new_rp.glyphs)):
        d = dy if t in moved else 0.0
        if (
            a.code != b.code
            or abs(a.origin[0] - b.origin[0]) > 0.01
            or abs(a.origin[1] - d - b.origin[1]) > 0.01
            or (a.paint.font, a.paint.size, a.paint.fill, a.paint.Tr) != (b.paint.font, b.paint.size, b.paint.fill, b.paint.Tr)
            or not _close(a.paint.trm2, b.paint.trm2, 1e-6)
        ):
            fail("text on the page would have changed")
    # And every shape and picture.
    moved_paths = {p.index for p in shift.pieces if p.kind in ("path", "clip")}
    moved_images = {p.index for p in shift.pieces if p.kind == "image"}
    if len(new_rp.paths) != len(rp.paths) or len(new_rp.images) != len(rp.images):
        fail("the page's shapes would have changed")
    for i, (a, b) in enumerate(zip(rp.paths, new_rp.paths)):
        d = dy if i in moved_paths else 0.0
        x0, y0, x1, y1 = a.bbox
        if a.op != b.op or a.clip != b.clip or not _close(b.bbox, (x0, y0 - d, x1, y1 - d), 0.01):
            fail("the page's shapes would have changed")
    for i, (a, b) in enumerate(zip(rp.images, new_rp.images)):
        d = dy if i in moved_images else 0.0
        x0, y0, x1, y1 = a.bbox
        if not _close(b.bbox, (x0, y0 - d, x1, y1 - d), 0.01):
            fail("the page's pictures would have changed")

    # MuPDF's own list of shapes agrees.
    moved_boxes = [rp.paths[i].bbox for i in moved_paths]
    expected = []
    for look, r in _drawings(old_page):
        if any(_close(r, m, 0.15) for m in moved_boxes):
            r = (r[0], r[1] - dy, r[2], r[3] - dy)
        expected.append((look, r))
    unused = _drawings(new_page)
    if len(unused) != len(expected):
        fail("the page's lines and shapes changed")
    for look, rect in expected:
        k = next((i for i, (l, r) in enumerate(unused) if l == look and _close(r, rect, 0.15)), None)
        if k is None:
            fail("the page's lines and shapes changed")
        unused.pop(k)
    check_links(old_page, new_page, shift.links)

    # Nothing outside the block's old and new places changes, in two engines.
    x0, y0, x1, y1 = shift.old_box
    pad = (-2, -2, 2, 2)
    old_area = to_display(old_page, shift.old_box) + pad
    new_area = to_display(old_page, (x0, y0 - dy, x1, y1 - dy)) + pad
    boxes = [tuple(old_area), tuple(new_area)]
    old_mu, old_pf = render(old_bytes, page_no)
    new_mu, new_pf = render(new_bytes, page_no)
    if outside(old_mu, new_mu, boxes, 2.0, 0) or outside(old_pf, new_pf, boxes, 2.0, 1):
        fail("other parts of the page would have changed")

    # The block looks the same at its new place: rendered at a zoom that moves it by a
    # whole number of pixels, so each glyph lands on the same fraction of a pixel.
    a = to_display(old_page, (x0, y0, x0, y0))
    b = to_display(old_page, (x0, y0 - dy, x0, y0 - dy))
    vx, vy = b.x0 - a.x0, b.y0 - a.y0
    length = math.hypot(vx, vy)
    zoom = max(1, round(2 * length)) / length
    sx, sy = round(vx * zoom), round(vy * zoom)
    # Drawn alone, without what stays put (a background picture behind it, text beside
    # it), which the checks above already proved unchanged.
    old_pieces = _pieces(rp)
    moving_ids = {(p.kind, p.start) for p in shift.pieces}
    keep = {n for n, p in enumerate(old_pieces) if (p.kind, p.start) in moving_ids}
    old_mu, old_pf = render(_only(old_bytes, page_no, keep), page_no, zoom)
    new_mu, new_pf = render(_only(new_bytes, page_no, keep), page_no, zoom)
    # PDFium rounds glyph positions to whole pixels: a pixel of slack, as in the other checks.
    for before, after, slack in ((old_mu, new_mu, 0), (old_pf, new_pf, 1)):
        if _differs_after_shift(before, after, old_area, zoom, sx, sy, slack):
            fail("the moved text wouldn't look exactly the same")

    # Every line reads the same: the moved ones at their new place.
    vector = (vx, vy)
    expect = sorted(_line_key(l, vector if l.id in shift.lines else (0.0, 0.0)) for l in lines)
    got = sorted(_line_key(l, (0.0, 0.0)) for l in page_lines(new_page))
    if len(expect) != len(got) or any(e[0] != g[0] or not _close(e[1:], g[1:], 0.3) for e, g in zip(expect, got)):
        fail("the moved text would read differently")
    check_other_pages(old_bytes, new_bytes, {page_no})
    return [(page_no, *boxes[0]), (page_no, *boxes[1])]


def _only(doc_bytes: bytes, page_no: int, keep: set[int]) -> bytes:
    """The file with only some of the page's drawn pieces (by their order on the page:
    the same before and after a move, which only adds cm instructions). Clipping paths
    and state changes outside the pieces stay."""
    pdf = pikepdf.open(io.BytesIO(doc_bytes))
    rp = replay(pdf, page_no)
    drop: set[int] = set()
    for n, p in enumerate(_pieces(rp)):
        if p.kind != "clip" and n not in keep:
            drop.update(range(p.start, p.end + 1))
    write_page(pdf, page_no, [ins for k, ins in enumerate(rp.instructions) if k not in drop])
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _line_key(line: Line, vector) -> tuple:
    x0, y0, x1, y1 = line.bbox
    return (line.text, round(x0 + vector[0], 1), round(y0 + vector[1], 1), round(x1 + vector[0], 1), round(y1 + vector[1], 1))


def _differs_after_shift(before, after, area, zoom: float, sx: int, sy: int, slack: int = 0) -> bool:
    """Do the pixels of `area` (as displayed) differ from those sx, sy pixels further on?
    With slack, a pixel may match any of its neighbours instead."""
    h, w = before.shape
    x0, y0 = max(0, int(area.x0 * zoom)), max(0, int(area.y0 * zoom))
    x1, y1 = min(w, int(area.x1 * zoom) + 1), min(h, int(area.y1 * zoom) + 1)
    # Keep both windows on the page.
    x0, x1 = max(x0, -sx), min(x1, w - sx)
    y0, y1 = max(y0, -sy), min(y1, h - sy)
    if x1 <= x0 or y1 <= y0:
        return False
    a = before[y0:y1, x0:x1].astype(int)
    b = after[y0 + sy : y1 + sy, x0 + sx : x1 + sx].astype(int)
    if not slack:
        return int((np.abs(a - b) > 24).sum()) > 0
    return outside(a, b, [], 1.0, slack) > 0


def shift_below(src: bytes, page_no: int, anchor: Box, size: float, cut: float, dy: float) -> tuple[bytes, list, int, Shift] | None:
    """Move everything in the anchor's column below `cut` (PDF y) down by dy (up when
    negative). Returns (the new file, the areas that changed, how many lines moved, the
    shift), or None when there's nothing below. Raises MoveRefused."""
    doc = pymupdf.open("pdf", src)
    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported:
        raise MoveRefused("the page is built in a way that can't be moved safely yet")
    page = doc[page_no]
    if not matches_trace(rp, page) or rp.stray_restore or rp.open_text:
        raise MoveRefused("the page is built in a way that can't be moved safely yet")
    lines = page_lines(page)
    shift = plan(pdf, doc, page_no, rp, lines, anchor, size, cut, dy)
    if shift is None:
        return None
    apply(pdf, page_no, rp, shift)
    out = io.BytesIO()
    pdf.save(out)
    new = out.getvalue()
    try:
        areas = verify(src, new, page_no, rp, shift, lines)
    except EditRejected as e:
        raise MoveRefused("the text below couldn't be moved exactly") from e  # the check that failed, for debugging
    return new, areas, len(shift.lines), shift
