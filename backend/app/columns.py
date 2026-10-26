"""Table columns: add one beside a column (it takes the room the others have to spare, or
the page's room beside the table), or delete one (the table gets narrower, as in Word).
Adding is described with its code, below.

A bordered table is drawn as rectangles: each cell's shading and clipping, thin bars on the
column edges, bars between them along the rows, little squares where they meet. Deleting
column k takes its text away (as deleting lines does: underlines and links with it), then
its rectangles and pictures, and the bars of one of its edges (its right one, or its left
one for the last column, so the table keeps its outer border); everything to its right
moves left by exactly the column's width. Rectangles move by having their numbers
rewritten; text and pictures move whole, each inside q ... Q with a translation, as a
dragged block does (dragging.py). Nothing else on the page is touched.

Checked before it's accepted: every glyph, shape and picture is where it was or exactly
one column's width to the left; MuPDF lists the same shapes, minus the column's, the
others in their new places; nothing outside the table changes by a pixel; the table's
part left of the column is pixel for pixel as it was, and its part right of the column
pixel for pixel as it was, one column's width further left (MuPDF and PDFium); every line
reads the same, the moved ones at their new place; and the page reads as a table with
one column fewer, the same rows, the same text in each cell.

Refused, with the reason: a table without borders, merged cells, a table's only column,
a turned page, rectangles drawn some other way, or something drawn across the table's
edge.
"""

import io
import math
from dataclasses import dataclass

import pikepdf
import pymupdf

from . import moving
from .changes import EditRejected, LinkChange, _drawings, check_links, check_other_pages, fail, outside, page_links, render, to_display, to_pdf, write_page
from .deleting import delete_lines
from .editing import EditResult, comparable
from .moving import Piece, _lasting
from .pdfcontent import Replay, Unsupported, apply, inverse, matches_trace, replay
from .tables import Table, find_tables
from .textlayout import Line, page_lines

THIN = 3.0  # points: a bar this narrow is a border on a column edge
PAD = 2.0  # points: what's drawn this far outside the table's borders is still its own


def _op(operands, op: str):
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(op))


def _open(src: bytes, page_no: int):
    doc = pymupdf.open("pdf", src)
    if not 0 <= page_no < doc.page_count:
        raise EditRejected("That page doesn't exist.")
    page = doc[page_no]
    if page.rotation:
        raise EditRejected("Tables on turned pages can't be changed yet.")
    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    if not matches_trace(rp, page) or rp.stray_restore or rp.open_text:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    return doc, page, pdf, rp


@dataclass
class Spot:
    table: Table
    col: int  # 0 = the leftmost column on the page, whatever the table's direction


def locate(page: pymupdf.Page, lines: list[Line], line_id: str, old_text: str) -> Spot:
    """The column a picked line is in."""
    line = next((l for l in lines if l.id == line_id), None)
    if line is None or comparable(line.text) != comparable(old_text):
        raise EditRejected("This line changed since you selected it. Select it again.")
    for table in find_tables(page, lines):
        for cell in table.cells:
            if any(l.id == line_id for l in cell.lines):
                if not table.ruled:
                    raise EditRejected("This table has no borders, so its columns can't be told apart exactly yet.")
                if any(c.colspan > 1 or c.rowspan > 1 for c in table.cells):
                    raise EditRejected("This table has merged cells, so its columns can't be changed yet.")
                return Spot(table, cell.col)
    raise EditRejected("This line isn't in a table.")


def _edges(page: pymupdf.Page, table: Table) -> list[float]:
    """The column edges, as PDF x."""
    return [to_pdf(page, (x, table.bbox[1], x, table.bbox[1]))[0] for x in table.cols]


def _rect_of(rp: Replay, p: Piece) -> tuple[int, list[float]] | None:
    """A path drawn as one rectangle in plain coordinates: (the index of its re, its
    operands); None otherwise."""
    if p.kind not in ("path", "clip") or abs(p.ctm[1]) > 1e-9 or abs(p.ctm[2]) > 1e-9:
        return None
    path = rp.paths[p.index]
    if len(path.subpaths) != 1:
        return None
    res = [k for k in range(p.start, p.end + 1) if not isinstance(rp.instructions[k], pikepdf.ContentStreamInlineImage) and str(rp.instructions[k].operator) == "re"]
    builds = [k for k in range(p.start, p.end + 1) if not isinstance(rp.instructions[k], pikepdf.ContentStreamInlineImage) and str(rp.instructions[k].operator) in ("m", "l", "c", "v", "y", "h", "re")]
    if len(res) != 1 or builds != res:
        return None
    return res[0], [float(v) for v in rp.instructions[res[0]].operands]


def _shifted_re(ctm, operands: list[float], dx: float) -> list[float]:
    """A rectangle's operands, moved dx to the right on the page."""
    x, y, w, h = operands
    return [x + dx / ctm[0], y, w, h]


@dataclass
class Plan:
    rp: Replay
    drop: set[int]  # instructions taken out
    rects: dict[int, list[float]]  # re instruction -> its new operands
    moves: list[Piece]  # text and pictures moved left
    shifted: dict[int, float]  # path index -> how far it moved (PDF x)
    dropped_paths: set[int]
    width: float  # the column's width (PDF)
    blanks: set[int]  # glyphs taken away with the column: spaces in its empty cells
    kept_state: dict[int, list]  # instruction -> what a dropped text object set, set again after it


def _plan(page: pymupdf.Page, rp: Replay, table: Table, col: int) -> Plan:
    edges = _edges(page, table)
    k = col
    width = edges[k + 1] - edges[k]
    last = k == len(edges) - 2
    gone_edge = k if last else k + 1  # the edge whose bars go
    x0, y0, x1, y1 = to_pdf(page, table.bbox)
    box = (x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
    inside = lambda b: b[0] >= box[0] and b[1] >= box[1] and b[2] <= box[2] and b[3] <= box[3]
    touches = lambda b: min(b[2], box[2]) > max(b[0], box[0]) and min(b[3], box[3]) > max(b[1], box[1])

    def shift_at(x: float) -> float:
        """How far a point moves: nothing left of the column, a column's width right of it."""
        return -width if x >= edges[k + 1] - THIN else 0.0

    pieces = moving._pieces(rp)
    drop, rects, moves, shifted, dropped_paths = set(), {}, [], {}, set()
    blanks: set[int] = set()
    kept_state: dict[int, list] = {}
    for p in pieces:
        if not touches(p.box):
            continue
        if p.kind == "text":
            # By where its letters are: text just above or below the table isn't in it.
            seen = [x0 <= (b[0] + b[2]) / 2 <= x1 and y0 <= (b[1] + b[3]) / 2 <= y1 for t in p.glyphs if not moving._blank(rp, rp.glyphs[t]) for b in [moving._glyph_box(rp.glyphs[t])]]
            if seen and not any(seen):
                continue
            if seen and not all(seen):
                raise EditRejected("This column can't be deleted: text is drawn across the table's edge.")
        elif not inside(p.box):
            if p.kind == "clip" or (p.kind == "path" and p.background and p.box[0] <= box[0] and p.box[2] >= box[2]):
                continue  # clipping, or a background the whole table is on: stays
            what = "text" if p.kind == "text" else "a picture" if p.kind == "image" else "a line or a shape"
            raise EditRejected(f"This column can't be deleted: {what} is drawn across the table's edge.")
        cx = (p.box[0] + p.box[2]) / 2
        if p.kind == "text":
            # By its visible letters: Word puts the spaces of empty cells further along
            # the row in the same text object.
            letters = [moving._glyph_box(rp.glyphs[t]) for t in p.glyphs if not moving._blank(rp, rp.glyphs[t])]
            if letters:
                where = {"in" if edges[k] < (b[0] + b[2]) / 2 < edges[k + 1] else shift_at((b[0] + b[2]) / 2) for b in letters}
                if len(where) > 1:
                    raise EditRejected("This column's text is drawn together with its neighbours', so they can't be moved apart yet.")
                if where == {"in"}:
                    raise EditRejected("This column's text couldn't be taken out exactly.")
                if where == {-width}:
                    moves.append(p)
                continue
        if p.kind in ("text", "image"):
            if edges[k] < cx < edges[k + 1]:
                if p.kind == "image":
                    drop.add(p.start)
                    continue
                if not all(moving._blank(rp, rp.glyphs[t]) for t in p.glyphs):
                    raise EditRejected("This column's text couldn't be taken out exactly.")
                # Spaces Word draws in an empty cell: they go too; what they set stays set.
                lasting = _lasting(rp, p)
                if lasting is None:
                    raise EditRejected("This column's text couldn't be taken out exactly.")
                drop.update(range(p.start, p.end + 1))
                blanks.update(p.glyphs)
                kept_state[p.end] = lasting
                continue
            d = shift_at(cx)
            if d:
                moves.append(p)
            continue
        found = _rect_of(rp, p)
        if found is None:
            raise EditRejected("This table's lines are drawn in a way that can't be changed yet.")
        re_at, operands = found
        narrow = p.box[2] - p.box[0] <= THIN
        if narrow:
            j = min(range(len(edges)), key=lambda e: abs(edges[e] - cx))
            if abs(edges[j] - cx) > THIN:
                raise EditRejected("Some rows of this table are split into columns differently, so its columns can't be changed yet.")
            if j == gone_edge:
                drop.update(range(p.start, p.end + 1))
                dropped_paths.add(p.index)
                continue
            d = 0.0 if j <= k else -width
            if last and j == k + 1:
                d = -width  # the table's right border takes the gone edge's place
        else:
            if edges[k] < cx < edges[k + 1]:
                drop.update(range(p.start, p.end + 1))
                dropped_paths.add(p.index)
                continue
            a, b = shift_at(p.box[0]), shift_at(p.box[2])
            if a != b:
                # Across the column (a line along the whole table): shorter by its width,
                # its left end staying.
                ox, oy, ow, oh = operands
                ea, ee = p.ctm[0], p.ctm[4]
                left = min(ox * ea + ee, (ox + ow) * ea + ee)
                right = max(ox * ea + ee, (ox + ow) * ea + ee) - width
                rects[re_at] = [(left - ee) / ea, oy, (right - left) / ea, oh]
                shifted[p.index] = 0.0
                continue
            d = a
        if d:
            rects[re_at] = _shifted_re(p.ctm, operands, d)
        shifted[p.index] = d
    return Plan(rp, drop, rects, moves, shifted, dropped_paths, width, blanks, kept_state)


def _rewrite(pdf: pikepdf.Pdf, page_no: int, plan: Plan) -> None:
    rp = plan.rp
    before: dict[int, list] = {}
    after: dict[int, list] = {}
    for p in plan.moves:
        lasting = _lasting(rp, p)
        if lasting is None:
            raise EditRejected("This column's neighbours can't be moved exactly.")
        go, _back = moving._cm(p, 0.0, -plan.width)
        before.setdefault(p.start, []).extend([_op([], "q"), _op(go, "cm")])
        after.setdefault(p.end, [])[0:0] = [_op([], "Q"), *lasting]
    out = []
    for k, ins in enumerate(rp.instructions):
        if k in plan.drop:
            out += plan.kept_state.get(k, [])
            continue
        out += before.get(k, [])
        if k in plan.rects:
            ins = _op(plan.rects[k], "re")
        out.append(ins)
        out += after.get(k, [])
    write_page(pdf, page_no, out)


def _links(pdf: pikepdf.Pdf, page_no: int, rp: Replay, plan: Plan) -> list[LinkChange]:
    moved = {t for p in plan.moves for t in p.glyphs}
    changes = []
    annots = pdf.pages[page_no].obj.get("/Annots")
    for link in page_links(pdf, page_no, rp):
        under = link.glyphs & moved
        if not under:
            continue
        if under != link.glyphs:
            raise EditRejected("A link covers text in two columns, so they can't be moved apart.")
        a = annots[link.index]
        x0, y0, x1, y1 = link.rect
        a.Rect = pikepdf.Array([x0 - plan.width, y0, x1 - plan.width, y1])
        if "/QuadPoints" in a:
            q = [float(v) for v in a.QuadPoints]
            a.QuadPoints = pikepdf.Array([v - plan.width if i % 2 == 0 else v for i, v in enumerate(q)])
        changes.append(LinkChange(link, (x0 - plan.width, y0, x1 - plan.width, y1)))
    return changes


def delete_column(src: bytes, page_no: int, line_id: str, old_text: str) -> EditResult:
    """The column a line is in, taken out of its table; the columns right of it move left."""
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    spot = locate(page, lines, line_id, old_text)
    table, col = spot.table, spot.col
    if len(table.cols) <= 2:
        raise EditRejected("This is the table's only column. Delete its lines instead.")

    # 1. Its text, with its underlines and links, as deleting lines does (checked there).
    column = [l for c in table.cells if c.col == col for l in c.lines]
    step = src
    if column:
        step = delete_lines(src, [(page_no, l.id, l.text) for l in column], close_gap=False).pdf

    # 2. Its rectangles and pictures; what's right of it moves left.
    doc, page, pdf, rp = _open(step, page_no)
    lines = page_lines(page)
    # The same borders: the table as found before, unless it reads with its text gone
    # (a table left with pictures only no longer reads as one).
    again = [t for t in find_tables(page, lines) if t.ruled and abs(t.bbox[1] - table.bbox[1]) < 1 and abs(t.bbox[0] - table.bbox[0]) < 1]
    spot = Spot(again[0] if again else table, col)
    plan = _plan(page, rp, spot.table, col)
    _rewrite(pdf, page_no, plan)
    links = _links(pdf, page_no, rp, plan)
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()
    area = _verify(step, result, page_no, plan, spot.table, col, lines, links)

    x0, y0, x1, y1 = spot.table.bbox
    mark = (x0, y0, x1 - _display_width(page, plan.width), y1)
    return EditResult(result, [], "", [(page_no, *area)], [(page_no, *mark)])


def _display_width(page: pymupdf.Page, width: float) -> float:
    a = to_display(page, (0, 0, 0, 0))
    b = to_display(page, (width, 0, width, 0))
    return abs(b.x0 - a.x0)


def _verify(old: bytes, new: bytes, page_no: int, plan: Plan, table: Table, col: int, lines: list[Line], links: list[LinkChange]) -> tuple:
    """Check the column's removal; return the area (as displayed) outside which nothing changed."""
    rp = plan.rp
    old_doc, new_doc = pymupdf.open("pdf", old), pymupdf.open("pdf", new)
    old_page, new_page = old_doc[page_no], new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    if not matches_trace(new_rp, new_page):
        fail("the page can't be read back")
    w = plan.width

    # Every glyph where it was, or one column's width to the left.
    moved = {t for p in plan.moves for t in p.glyphs}
    remaining = [(t, g) for t, g in enumerate(rp.glyphs) if t not in plan.blanks]
    if len(new_rp.glyphs) != len(remaining):
        fail("the text on the page would have changed")
    for (t, a), b in zip(remaining, new_rp.glyphs):
        d = -w if t in moved else 0.0
        if a.code != b.code or abs(a.origin[0] + d - b.origin[0]) > 0.01 or abs(a.origin[1] - b.origin[1]) > 0.01 or (a.paint.font, a.paint.size, a.paint.fill) != (b.paint.font, b.paint.size, b.paint.fill):
            fail("the text on the page would have changed")
    # Every shape kept is where it was or moved left; the column's are gone.
    kept = [(i, p) for i, p in enumerate(rp.paths) if i not in plan.dropped_paths]
    if len(kept) != len(new_rp.paths):
        fail("the table's lines would have changed wrongly")
    for (i, a), b in zip(kept, new_rp.paths):
        x0, y0, x1, y1 = a.bbox
        d = plan.shifted.get(i, 0.0)
        expect = (x0 + d, y0, x1 + d, y1)
        if i in plan.shifted and d == 0.0 and abs((a.bbox[2] - a.bbox[0]) - (b.bbox[2] - b.bbox[0])) > 0.01:
            expect = (x0, y0, x1 - w, y1)  # a line across the column, shorter
        if a.op != b.op or a.clip != b.clip or not moving._close(b.bbox, expect, 0.01):
            fail("the table's lines would have changed wrongly")
    moved_images = {p.index for p in plan.moves if p.kind == "image"}
    dropped_images = {i for i, im in enumerate(rp.images) if im.op in plan.drop}
    kept_images = [(i, im) for i, im in enumerate(rp.images) if i not in dropped_images]
    if len(kept_images) != len(new_rp.images):
        fail("the page's pictures would have changed")
    for (i, a), b in zip(kept_images, new_rp.images):
        d = -w if i in moved_images else 0.0
        x0, y0, x1, y1 = a.bbox
        if not moving._close(b.bbox, (x0 + d, y0, x1 + d, y1), 0.01):
            fail("the page's pictures would have changed")
    check_links(old_page, new_page, links)

    # Nothing outside the table changes, in two engines.
    tb = pymupdf.Rect(table.bbox) + (-PAD - 1, -PAD - 1, PAD + 1, PAD + 1)
    old_mu, old_pf = render(old, page_no)
    new_mu, new_pf = render(new, page_no)
    if outside(old_mu, new_mu, [tuple(tb)], 2.0, 0) or outside(old_pf, new_pf, [tuple(tb)], 2.0, 1):
        fail("other parts of the page would have changed")

    # Inside it: left of the column as it was; right of it as it was, a column further left.
    edges = table.cols
    wd = _display_width(old_page, w)
    zoom = max(1, round(2 * wd)) / wd
    # The table itself, not the text just above or below it (which stays put).
    x0, y0, x1, y1 = table.bbox
    left = pymupdf.Rect(x0 - 0.6, y0 - 0.6, edges[col] - THIN, y1 + 0.6)
    right = pymupdf.Rect(edges[col + 1] + THIN, y0 - 0.6, x1 + 0.6, y1 + 0.6)

    def differs(z: float, allow: int = 0) -> list[bool]:
        sx = -round(wd * z)
        old_mu, old_pf = render(old, page_no, z)
        new_mu, new_pf = render(new, page_no, z)
        out = []
        for before, after, slack, noise in ((old_mu, new_mu, 0, 0), (old_pf, new_pf, 1, allow)):
            out.append(left.width > 0 and moving._differs_after_shift(before, after, left, z, 0, 0, slack, noise))
            out.append(right.width > 0 and moving._differs_after_shift(before, after, right, z, sx, 0, slack, noise))
        return out

    first = differs(zoom)
    if any(first):
        # A thin bar or a letter exactly between two pixels can round either way; at twice
        # the size it's on a pixel. Each part, in each engine, must match at one size:
        # MuPDF exactly, PDFium (which rounds each letter to a whole pixel) within a few
        # pixels, once MuPDF has matched exactly.
        again = differs(2 * zoom, allow=4 if not any(first[:2]) else 0)
        if any(a and b for a, b in zip(first, again)):
            fail("the table's other columns wouldn't look exactly the same")

    # Every line reads the same, the moved ones at their new place.
    # A line moves when its letters do (a space of it, in an empty cell further on, may too).
    letters = lambda l: {t for t in l.members if not moving._blank(rp, rp.glyphs[t])}
    moved_ids = {l.id for l in lines if letters(l) and letters(l) <= moved}
    expect = sorted(moving._line_key(l, (-wd, 0.0) if l.id in moved_ids else (0.0, 0.0)) for l in lines)
    got = sorted(moving._line_key(l, (0.0, 0.0)) for l in page_lines(new_page))
    if len(expect) != len(got) or any(e[0] != g[0] or not moving._close(e[1:], g[1:], 0.3) for e, g in zip(expect, got)):
        fail("the table's text would read differently")
    # The page reads as the table with one column fewer (when it has text left to read,
    # and more than one column: a single column doesn't read as a table).
    if not any(c.lines for c in table.cells if c.col != col) or len(table.cols) <= 3:
        check_other_pages(old, new, {page_no})
        return tuple(tb)
    after = [t for t in find_tables(new_page, page_lines(new_page)) if t.ruled and abs(t.bbox[1] - table.bbox[1]) < 1]
    want_cols = [x for i, x in enumerate(edges) if i != (col + 1 if col < len(edges) - 2 else col)]
    want_cols = [x if x <= edges[col] + 0.5 else x - wd for x in want_cols]
    if len(after) != 1 or len(after[0].cols) != len(want_cols) or not moving._close(after[0].cols, want_cols, 0.6) or not moving._close(after[0].rows, table.rows, 0.6):
        fail("the page wouldn't read as the same table with one column fewer")
    old_text = sorted((c.row, c.col if c.col < col else c.col - 1, comparable(" ".join(l.text for l in c.lines))) for c in table.cells if c.col != col)
    new_text = sorted((c.row, c.col, comparable(" ".join(l.text for l in c.lines))) for c in after[0].cells)
    if old_text != new_text:
        fail("the table's cells would read differently")
    check_other_pages(old, new, {page_no})
    return tuple(tb)


# Adding a column -------------------------------------------------------------------------------
#
# The new column takes its width from the page beside the table, if there's room, and from
# the empty space in the other columns: each gives up whole points of it, its text keeping
# the side of the cell it keeps to (or its middle), with at least the table's own margin.
# Its borders and shading are those of the column it's added beside: drawn again, right
# after them, in the new column's place; the edge between them gets that column's bars.


MIN_WIDTH = 24.0  # points: a narrower new column isn't worth it


def _side(cell, line) -> str:
    """How a line sits in its cell: "left", "right" or "center" (as rows._placing tells)."""
    left, right = line.bbox[0] - cell.bbox[0], cell.bbox[2] - line.bbox[2]
    if abs(left - right) <= max(2.0, 0.1 * (cell.bbox[2] - cell.bbox[0])) and left > 2:
        return "center"
    return "left" if left <= right else "right"


def _margin(table: Table) -> float:
    """The table's own margin inside its cells: the usual gap on the side text keeps to."""
    gaps = sorted(
        (l.bbox[0] - c.bbox[0]) if _side(c, l) == "left" else (c.bbox[2] - l.bbox[2])
        for c in table.cells for l in c.lines if _side(c, l) != "center"
    )
    return max(2.0, gaps[len(gaps) // 2]) if gaps else 5.4


def _spare(table: Table, col: int, margin: float, pictures=()) -> float:
    """How many whole points a column can give up, its text keeping its side and margin,
    its pictures (which keep their left side) still fitting."""
    widths = []
    x0, x1 = table.cols[col], table.cols[col + 1]
    for b in pictures:
        if x0 <= (b[0] + b[2]) / 2 <= x1:
            widths.append(x1 - b[2] - margin)
    for c in table.cells:
        if c.col != col:
            continue
        for l in c.lines:
            left, right = l.bbox[0] - c.bbox[0], c.bbox[2] - l.bbox[2]
            side = _side(c, l)
            widths.append(right - margin if side == "left" else left - margin if side == "right" else left + right - 2 * margin)
    if not widths:
        return math.floor(max(0.0, (x1 - x0) - 2 * margin - MIN_WIDTH))
    return max(0, math.floor(min(widths)))


@dataclass
class Reshape:
    """Old edges -> new edges (as displayed); the new column, if any, at `added`."""

    old: list[float]
    new: list[float]
    cols: list[int]  # old column -> its index now
    edges: list[int]  # old edge -> its index now
    added: int | None = None  # the new column's index
    model: int | None = None  # the old column whose look it takes
    model_edge: int | None = None  # the old edge whose bars the new edge gets
    missing_edge: int | None = None  # the new edge that has no old one


def _widths_for_new_column(page: pymupdf.Page, table: Table, lines: list[Line], col: int, where: str) -> Reshape:
    n = len(table.cols) - 1
    old = list(table.cols)
    widths = [old[i + 1] - old[i] for i in range(n)]
    margin = _margin(table)
    t0, t1 = table.bbox[1], table.bbox[3]
    pictures = [tuple(im["bbox"]) for im in page.get_image_info() if t0 <= (im["bbox"][1] + im["bbox"][3]) / 2 <= t1]
    spare = [_spare(table, i, margin, pictures) for i in range(n)]
    # Room on the page beside the table: on its right (or, for a right-to-left table, its left).
    columns = [l.column for l in lines if l.column]
    area = (min(c[0] for c in columns), max(c[1] for c in columns)) if columns else (old[0], old[-1])
    beside = math.floor(max(0.0, (old[0] - area[0]) if table.rtl else (area[1] - old[-1])))
    if beside < MIN_WIDTH:
        beside = 0  # a sliver: a table as wide as the text keeps its edge in line with it
    wanted = max(MIN_WIDTH, math.floor(min(widths)))
    width = min(wanted, beside + sum(spare))
    if width < MIN_WIDTH:
        raise EditRejected("There's no room for another column: the table is as wide as the page and its columns have no empty space to give.")
    grow = min(beside, width)
    need = width - grow
    give = [0] * n
    total = sum(spare)
    if need:
        give = [math.floor(need * s / total) for s in spare]
        for i in sorted(range(n), key=lambda i: spare[i] - give[i], reverse=True):
            if sum(give) >= need:
                break
            if give[i] < spare[i]:
                give[i] += 1
    new_widths = [w - g for w, g in zip(widths, give)]
    added = col + 1 if where == "right" else col
    new_widths.insert(added, float(width))
    if table.rtl:
        new = [old[-1]]
        for w in reversed(new_widths):
            new.insert(0, new[0] - w)
    else:
        new = [old[0]]
        for w in new_widths:
            new.append(new[-1] + w)
    cols = [i if i < added else i + 1 for i in range(n)]
    edges = [n + 1 if j == n else j if j <= added else j + 1 for j in range(n + 1)]
    missing = next(j for j in range(n + 2) if j not in edges)
    inner = [j for j in (col, col + 1) if 0 < j < n]
    model_edge = (col + 1 if where == "right" and col + 1 in inner else inner[0]) if inner else (col + 1 if where == "right" else col)
    return Reshape(old, new, cols, edges, added, col, model_edge, missing)


@dataclass
class ReshapePlan:
    rp: Replay
    rects: dict  # re instruction -> its new operands
    copies: dict  # instruction -> what's drawn again right after it
    moves: dict  # text or picture piece (its first instruction) -> how far right (PDF)
    pieces: list
    glyph_dx: dict
    path_boxes: list  # each path's expected box after, copies right after their original
    image_dx: dict
    old_box: tuple  # as displayed
    new_box: tuple


def _plan_reshape(page: pymupdf.Page, rp: Replay, table: Table, shape: Reshape) -> ReshapePlan:
    E = _edges(page, table)
    to_x = lambda x: to_pdf(page, (x, table.bbox[1], x, table.bbox[1]))[0]
    E2 = [to_x(x) for x in shape.new]
    n = len(E) - 1
    edge_dx = [E2[shape.edges[j]] - E[j] for j in range(n + 1)]
    left_dx = [E2[shape.cols[i]] - E[i] for i in range(n)]
    right_dx = [E2[shape.cols[i] + 1] - E[i + 1] for i in range(n)]
    x0, y0, x1, y1 = to_pdf(page, table.bbox)
    box = (x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
    inside = lambda b: b[0] >= box[0] and b[1] >= box[1] and b[2] <= box[2] and b[3] <= box[3]
    touches = lambda b: min(b[2], box[2]) > max(b[0], box[0]) and min(b[3], box[3]) > max(b[1], box[1])

    def column_at(x: float) -> int:
        for i in range(n):
            if E[i] - THIN <= x <= E[i + 1] + THIN:
                return i
        raise EditRejected("Some rows of this table are split into columns differently, so its columns can't be changed yet.")

    def end_dx(x: float) -> float:
        """How far a rectangle's end moves: with the edge it's on, or with the nearer side
        of its column (a cell's inner shading keeps its margin)."""
        j = min(range(n + 1), key=lambda e: abs(E[e] - x))
        if abs(E[j] - x) <= THIN:
            return edge_dx[j]
        i = column_at(x)
        return left_dx[i] if x - E[i] <= E[i + 1] - x else right_dx[i]

    # Cell text: each cell's lines move by how they sit in it.
    cell_of_line = {}
    for c in table.cells:
        for l in c.lines:
            cell_of_line[l.id] = c
    line_of_glyph = {}
    for c in table.cells:
        for l in c.lines:
            for t in l.members:
                line_of_glyph[t] = l

    def text_dx(c, l) -> float:
        i = c.col
        side = _side(c, l)
        return left_dx[i] if side == "left" else right_dx[i] if side == "right" else (left_dx[i] + right_dx[i]) / 2

    pieces = moving._pieces(rp)
    rects, copies, moves, glyph_dx, image_dx = {}, {}, {}, {}, {}
    new_boxes: dict = {}
    extra: dict = {}  # path index -> boxes of its copies
    scopes = moving._clip_scopes(rp)
    # Text and pictures first: a clip that's smaller than its cell goes with what it clips.
    pieces = sorted(pieces, key=lambda p: (p.kind in ("path", "clip"), p.start))
    for p in pieces:
        if not touches(p.box):
            continue
        if p.kind == "text":
            letters = [t for t in p.glyphs if not moving._blank(rp, rp.glyphs[t])]
            if not letters:
                # Only spaces: with their line (a number's narrow space, drawn on its own),
                # else they stay.
                ds = {round(text_dx(cell_of_line[line_of_glyph[t].id], line_of_glyph[t]), 4) for t in p.glyphs if t in line_of_glyph}
                if len(ds) == 1 and all(t in line_of_glyph for t in p.glyphs):
                    d = ds.pop()
                    if d:
                        moves[p.start] = d
                        for t in p.glyphs:
                            glyph_dx[t] = d
                continue
            ds = set()
            for t in letters:
                l = line_of_glyph.get(t)
                if l is None:
                    b = moving._glyph_box(rp.glyphs[t])
                    if x0 <= (b[0] + b[2]) / 2 <= x1 and y0 <= (b[1] + b[3]) / 2 <= y1:
                        raise EditRejected("Some text in this table couldn't be placed in its cell.")
                    ds.add(0.0)
                    continue
                ds.add(round(text_dx(cell_of_line[l.id], l), 4))
            if len(ds) > 1:
                raise EditRejected("This table's text is drawn across its cells, so its columns can't be changed yet.")
            d = ds.pop()
            if d:
                moves[p.start] = d
                for t in p.glyphs:
                    glyph_dx[t] = d
            continue
        if not inside(p.box):
            if p.kind == "clip" or (p.kind == "path" and p.background and p.box[0] <= box[0] and p.box[2] >= box[2]):
                continue
            what = "a picture" if p.kind == "image" else "a line or a shape"
            raise EditRejected(f"This table's columns can't be changed: {what} is drawn across its edge.")
        if p.kind == "image":
            i = column_at((p.box[0] + p.box[2]) / 2)
            d = left_dx[i]
            if d:
                moves[p.start] = d
            image_dx[p.index] = d
            continue
        found = _rect_of(rp, p)
        if found is None:
            raise EditRejected("This table's lines are drawn in a way that can't be changed yet.")
        re_at, operands = found
        a0, a1 = p.box[0], p.box[2]
        if p.kind == "clip":
            i = column_at((a0 + a1) / 2)
            if a0 > E[i] + THIN or a1 < E[i + 1] - THIN:
                end = scopes.get(p.index, len(rp.instructions))
                inner = {moves.get(q.start, 0.0) for q in pieces if q.kind in ("text", "image") and p.end < q.start < end}
                if len(inner) > 1:
                    raise EditRejected("This table's cells are drawn in a way that can't be changed yet.")
                d = inner.pop() if inner else left_dx[i]
                if d:
                    rects[re_at] = _re_operands(p.ctm, operands, a0 + d, a1 + d)
                new_boxes[p.index] = (a0 + d, p.box[1], a1 + d, p.box[3])
                continue
        if a1 - a0 <= THIN:
            j = min(range(n + 1), key=lambda e: abs(E[e] - (a0 + a1) / 2))
            if abs(E[j] - (a0 + a1) / 2) > THIN:
                raise EditRejected("Some rows of this table are split into columns differently, so its columns can't be changed yet.")
            d0 = d1 = edge_dx[j]
            if shape.model_edge == j and shape.missing_edge is not None:
                # This edge's bars, drawn again on the new edge.
                dd = E2[shape.missing_edge] - E[j]
                copies.setdefault(p.end, []).append(_re_moved(rp, p, operands, a0 + dd, a1 + dd))
                extra.setdefault(p.index, []).append((a0 + dd, p.box[1], a1 + dd, p.box[3]))
        else:
            i = column_at((a0 + a1) / 2)
            if a0 < E[i] - THIN or a1 > E[i + 1] + THIN:
                d0, d1 = end_dx(a0), end_dx(a1)  # across columns: each end with its own
            else:
                d0 = left_dx[i] if a0 - E[i] <= E[i + 1] - a0 else right_dx[i]
                d1 = right_dx[i] if E[i + 1] - a1 <= a1 - E[i] else left_dx[i]
            if shape.model == i and shape.added is not None and p.kind != "clip":
                # The column it's added beside: its shading and bars, drawn again in the new column.
                nl, nr = E2[shape.added], E2[shape.added + 1]
                c0 = nl + (a0 - E[i]) if a0 - E[i] <= E[i + 1] - a0 else nr - (E[i + 1] - a0)
                c1 = nr - (E[i + 1] - a1) if E[i + 1] - a1 <= a1 - E[i] else nl + (a1 - E[i])
                copies.setdefault(p.end, []).append(_re_moved(rp, p, operands, c0, c1))
                extra.setdefault(p.index, []).append((c0, p.box[1], c1, p.box[3]))
        if d0 or d1:
            rects[re_at] = _re_operands(p.ctm, operands, a0 + d0, a1 + d1)
        new_boxes[p.index] = (a0 + d0, p.box[1], a1 + d1, p.box[3])

    path_boxes = []
    for i, path in enumerate(rp.paths):
        path_boxes.append(new_boxes.get(i, path.bbox))
        path_boxes += extra.get(i, [])
    nb = (min(shape.new[0], table.bbox[0]), table.bbox[1], max(shape.new[-1], table.bbox[2]), table.bbox[3])
    return ReshapePlan(rp, rects, copies, moves, pieces, glyph_dx, path_boxes, image_dx, tuple(table.bbox), nb)


def _re_operands(ctm, operands: list, left: float, right: float) -> list:
    """A rectangle's operands for new left and right ends on the page (plain coordinates)."""
    x, y, w, h = operands
    a, e = ctm[0], ctm[4]
    p1, p2 = a * x + e, a * (x + w) + e
    q1, q2 = (left, right) if p1 <= p2 else (right, left)
    return [(q1 - e) / a, y, (q2 - q1) / a, h]


def _re_moved(rp: Replay, p: Piece, operands: list, left: float, right: float) -> list:
    """A rectangle drawn again, with new ends on the page, painted as the original."""
    return [_op(_re_operands(p.ctm, operands, left, right), "re"), rp.instructions[p.end]]


def _apply_reshape(pdf: pikepdf.Pdf, page_no: int, plan: ReshapePlan) -> None:
    rp = plan.rp
    by_start = {p.start: p for p in plan.pieces}
    before: dict = {}
    after: dict = {}
    for start, d in plan.moves.items():
        p = by_start[start]
        lasting = _lasting(rp, p)
        if lasting is None:
            raise EditRejected("This table's text can't be moved exactly.")
        go, _back = moving._cm(p, 0.0, d)
        before.setdefault(p.start, []).extend([_op([], "q"), _op(go, "cm")])
        after.setdefault(p.end, [])[0:0] = [_op([], "Q"), *lasting]
    out = []
    for k, ins in enumerate(rp.instructions):
        out += before.get(k, [])
        if k in plan.rects:
            ins = _op(plan.rects[k], "re")
        out.append(ins)
        out += after.get(k, [])
        for copy in plan.copies.get(k, []):
            out += copy
    write_page(pdf, page_no, out)


def _line_dx(rp: Replay, plan: ReshapePlan, l: Line) -> float:
    ds = {plan.glyph_dx.get(t, 0.0) for t in l.members if not moving._blank(rp, rp.glyphs[t])}
    return ds.pop() if len(ds) == 1 else 0.0


def _verify_reshape(old: bytes, new: bytes, page_no: int, plan: ReshapePlan, table: Table, shape: Reshape, lines: list) -> tuple:
    """Check the columns' new edges; return the area (as displayed) outside which nothing changed."""
    rp = plan.rp
    old_doc, new_doc = pymupdf.open("pdf", old), pymupdf.open("pdf", new)
    old_page, new_page = old_doc[page_no], new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    if not matches_trace(new_rp, new_page):
        fail("the page can't be read back")
    # Every glyph where it was, or moved along the row with its cell.
    if len(new_rp.glyphs) != len(rp.glyphs):
        fail("the text on the page would have changed")
    for t, (a, b) in enumerate(zip(rp.glyphs, new_rp.glyphs)):
        d = plan.glyph_dx.get(t, 0.0)
        if a.code != b.code or abs(a.origin[0] + d - b.origin[0]) > 0.01 or abs(a.origin[1] - b.origin[1]) > 0.01 or (a.paint.font, a.paint.size, a.paint.fill) != (b.paint.font, b.paint.size, b.paint.fill):
            fail("the text on the page would have changed")
    # Every shape where it should be: moved, stretched or drawn again in the new column.
    if len(new_rp.paths) != len(plan.path_boxes):
        fail("the table's lines would have changed wrongly")
    for b, want in zip(new_rp.paths, plan.path_boxes):
        if not moving._close(b.bbox, want, 0.02):
            fail("the table's lines would have changed wrongly")
    if len(new_rp.images) != len(rp.images):
        fail("the page's pictures would have changed")
    for i, (a, b) in enumerate(zip(rp.images, new_rp.images)):
        d = plan.image_dx.get(i, 0.0)
        x0, y0, x1, y1 = a.bbox
        if not moving._close(b.bbox, (x0 + d, y0, x1 + d, y1), 0.01):
            fail("the page's pictures would have changed")
    # Nothing outside the table's old and new places changes, in two engines.
    area = pymupdf.Rect(plan.old_box) | pymupdf.Rect(plan.new_box)
    area = area + (-PAD - 1, -PAD - 1, PAD + 1, PAD + 1)
    old_mu, old_pf = render(old, page_no)
    new_mu, new_pf = render(new, page_no)
    if outside(old_mu, new_mu, [tuple(area)], 2.0, 0) or outside(old_pf, new_pf, [tuple(area)], 2.0, 1):
        fail("other parts of the page would have changed")
    # Each cell's text looks the same where it went: drawn alone, each line compared with
    # itself moved along the row (moves go by half points: whole pixels at twice the size).
    keep_old = {k for k, p in enumerate(moving._pieces(rp)) if p.kind == "text"}
    keep_new = {k for k, p in enumerate(moving._pieces(new_rp)) if p.kind == "text"}
    alone_old, alone_new = moving._only(old, page_no, keep_old), moving._only(new, page_no, keep_new)

    def disp(d: float) -> float:
        return _display_width(old_page, abs(d)) * (1 if d >= 0 else -1)

    checks = [(pymupdf.Rect(l.bbox) + (-1, -1, 1, 1), disp(_line_dx(rp, plan, l))) for l in lines]

    def differs(z: float, allow: int = 0) -> list:
        omu, opf = render(alone_old, page_no, z)
        nmu, npf = render(alone_new, page_no, z)
        out = []
        for before, after_, slack, noise in ((omu, nmu, 0, 0), (opf, npf, 1, allow)):
            out.append(any(moving._differs_after_shift(before, after_, r, z, round(d * z), 0, slack, noise) for r, d in checks))
        return out

    first = differs(2.0)
    if any(first):
        again = differs(4.0, allow=4 if not first[0] else 0)
        if any(a and b for a, b in zip(first, again)):
            fail("the table's text wouldn't look exactly the same")
    # Every line reads the same, at its new place.
    expect = sorted(moving._line_key(l, (disp(_line_dx(rp, plan, l)), 0.0)) for l in lines)
    got = sorted(moving._line_key(l, (0.0, 0.0)) for l in page_lines(new_page))
    if len(expect) != len(got) or any(e[0] != g[0] or not moving._close(e[1:], g[1:], 0.3) for e, g in zip(expect, got)):
        fail("the table's text would read differently")
    # The page reads as the table with its new columns, each cell's text in its column.
    after = [t for t in find_tables(new_page, page_lines(new_page)) if t.ruled and abs(t.bbox[1] - table.bbox[1]) < 1]
    if len(after) != 1 or len(after[0].cols) != len(shape.new) or not moving._close(after[0].cols, shape.new, 0.6) or not moving._close(after[0].rows, table.rows, 0.6):
        fail("the page wouldn't read as the same table with its new columns")
    want = [(c.row, shape.cols[c.col], comparable(" ".join(l.text for l in c.lines))) for c in table.cells]
    if shape.added is not None:
        want += [(r, shape.added, "") for r in range(len(table.rows) - 1)]
    got_cells = sorted((c.row, c.col, comparable(" ".join(l.text for l in c.lines))) for c in after[0].cells)
    if sorted(want) != got_cells:
        fail("the table's cells would read differently")
    check_other_pages(old, new, {page_no})
    return tuple(area)


def _find_line(lines: list, text: str, near: tuple) -> Line:
    same = [l for l in lines if comparable(l.text) == comparable(text)]
    if not same:
        raise EditRejected("The table's text couldn't be found after its columns moved.")
    return min(same, key=lambda l: abs(l.bbox[1] - near[1]) + abs(l.bbox[0] - near[0]))


def add_column(src: bytes, page_no: int, line_id: str, old_text: str, where: str, texts: list) -> EditResult:
    """A new column left or right (on the page) of the column a line is in, looking like it,
    with `texts` in its cells, top to bottom (empty for an empty cell)."""
    from .adding import add_line
    from .rows import _placing
    from .tables import Cell

    if where not in ("left", "right"):
        raise EditRejected("A new column goes left or right of a column.")
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    spot = locate(page, lines, line_id, old_text)
    table, col = spot.table, spot.col
    rows = len(table.rows) - 1
    texts = [t.strip() for t in texts][:rows] + [""] * max(0, rows - len(texts))

    # 1. The columns make room; the new one gets the look of the one it's beside.
    shape = _widths_for_new_column(page, table, lines, col, where)
    plan = _plan_reshape(page, rp, table, shape)
    _apply_reshape(pdf, page_no, plan)
    out = io.BytesIO()
    pdf.save(out)
    current = out.getvalue()
    table_lines = [l for c in table.cells for l in c.lines]
    area = _verify_reshape(src, current, page_no, plan, table, shape, lines)
    areas = [(page_no, *area)]

    # 2. The text of each new cell, in the style of the cell beside it.
    warnings, lookalikes = [], []
    nx0, nx1 = shape.new[shape.added], shape.new[shape.added + 1]
    for r, text in enumerate(texts):
        if not text:
            continue
        cell = Cell(r, col, 1, 1, (nx0, table.rows[r], nx1, table.rows[r + 1]))
        placing = _placing(table, r, cell, table.rows[r])
        if placing is None:
            raise EditRejected("This table has no text to take a style from.")
        d = _line_dx(rp, plan, placing.line)
        dd = _display_width(page, abs(d)) * (1 if d >= 0 else -1)
        now = page_lines(pymupdf.open("pdf", current)[page_no])
        x0, y0, x1, y1 = placing.line.bbox
        ref = _find_line(now, placing.line.text, (x0 + dd, y0))
        try:
            result = add_line(current, page_no, text, ref.id, ref.text, at=(placing.x, placing.y), align=placing.align)
        except EditRejected as e:
            if "no room" in str(e).lower() or "too long" in str(e).lower():
                raise EditRejected(f"“{text}” doesn't fit in the new column (row {r + 1}). Shorten it, or make the column wider after.")
            raise
        current = result.pdf
        warnings += [w for w in result.warnings if w not in warnings]
        lookalikes += [l for l in result.lookalikes if l not in lookalikes]
        areas += result.areas
    return EditResult(current, warnings, " | ".join(t for t in texts if t), areas, [(page_no, *plan.new_box)], lookalikes)
