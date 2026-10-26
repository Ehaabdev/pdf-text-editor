"""Table columns: delete one (the table gets narrower, as in Word).

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
