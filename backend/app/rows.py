"""Table rows: a new row below or above a row, or a row deleted, as a word processor does.

A new row looks like the row it's added next to, as Word's Insert Below and Above do:
1. Room is made: everything below the spot, across the page, moves down one row's
   height (moving.py), the table's lower rows with it. Nothing goes onto the next page.
2. The row's borders and shading are drawn again in that room, copied from the row:
   its side borders, its shading and its bottom border (the top one is the row's own
   above it). Only the row's own pieces: a path that also draws other rows gives just
   its pieces in this row.
3. The text typed for each cell goes in, in the style of that cell's text (or of the
   nearest cell with text in its column), where it sits in its cell: from the cell's
   start, to its end, or centered.

Deleting a row: its text goes (as deleting lines does: links and underlines with it),
then its borders and shading but its top border, then everything below moves up.

Merged cells (a cell over several rows): a new row inside one makes it a row taller, and
deleting a row it covers makes it a row shorter; see _merged_across and below. Beside a
cell merged over several rows, a new row goes below its last row or above its first; a
row is deleted by clicking a cell one row tall.

Each step is checked as other changes are; the steps make one change, undone at once.
"""

import io
import math
from dataclasses import dataclass

import pikepdf
import pymupdf

from . import moving
from .adding import add_line
from .changes import check_other_pages, fail, outside, render, to_display, to_pdf, write_page
from .deleting import delete_lines
from .editing import EditRejected, EditResult, comparable
from .pdfcontent import Replay, Unsupported, inverse, matches_trace, mul, replay
from .tables import Cell, Table, find_tables
from .textlayout import Line, page_lines

THIN = 3.0  # a border line is at most this thick (points)
EDGE = 0.75  # a line this close to a row's edge (its middle) is that edge's border


def _open(src: bytes, page_no: int):
    doc = pymupdf.open("pdf", src)
    if not 0 <= page_no < doc.page_count:
        raise EditRejected("That page doesn't exist.")
    page = doc[page_no]
    if page.rotation:
        raise EditRejected("Rows of tables on turned pages can't be changed yet.")
    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported as e:
        raise EditRejected(str(e))
    if not matches_trace(rp, page) or rp.stray_restore or rp.open_text:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    return doc, page, pdf, rp


# Finding the row ----------------------------------------------------------------------------


@dataclass
class Spot:
    table: Table
    row: int
    cell: Cell | None = None  # the cell picked (it may be merged over several rows)

    @property
    def top(self) -> float:
        return self.table.rows[self.row]

    @property
    def bottom(self) -> float:
        return self.table.rows[self.row + 1]

    @property
    def cells(self) -> list[Cell]:
        """The row's cells, in the table's order (an Arabic table's first is on the right)."""
        cells = sorted((c for c in self.table.cells if c.row == self.row), key=lambda c: c.col)
        return cells[::-1] if self.table.rtl else cells


def locate(page: pymupdf.Page, lines: list[Line], line_id: str, old_text: str) -> Spot:
    """The table row a picked line is in."""
    line = next((l for l in lines if l.id == line_id), None)
    if line is None or comparable(line.text) != comparable(old_text):
        raise EditRejected("This line changed since you selected it. Select it again.")
    for table in find_tables(page, lines):
        for cell in table.cells:
            if any(l.id == line_id for l in cell.lines):
                if not table.ruled:
                    raise EditRejected("This table has no borders, so its rows can't be told apart exactly yet.")
                return Spot(table, cell.row, cell)
    raise EditRejected("This line isn't in a table.")


# Merged cells -------------------------------------------------------------------------------
#
# A cell merged over the rows on both sides of a new row gets a row taller instead of a new
# cell (as in Word): its rectangles that run across the spot get taller, their top staying.
# Deleting a row a merged cell covers makes it a row shorter. Its text stays where it sits
# in it: at its top, in its middle (moved half a row) or at its bottom.


def _merged_across(table: Table, edge: int) -> list[Cell]:
    """The cells merged over the rows on both sides of row edge `edge` (an index into
    table.rows): a new row there goes inside them."""
    return [c for c in table.cells if c.row < edge < c.row + c.rowspan]


def _row_cells(table: Table, row: int, skip: list[Cell]) -> list[Cell]:
    """The cells a new row like `row` has: one under each cell of that row (a merged one
    from a row above too), but those in `skip`; in the table's order."""
    cells = sorted((c for c in table.cells if c.row <= row < c.row + c.rowspan and all(c is not k for k in skip)), key=lambda c: c.col)
    return cells[::-1] if table.rtl else cells


def _re_rows(ctm, operands: list, bottom: float, top: float) -> list:
    """A rectangle's operands for a new bottom and top on the page (plain coordinates)."""
    x, y, w, h = operands
    d, f = ctm[3], ctm[5]
    p1, p2 = d * y + f, d * (y + h) + f
    q1, q2 = (bottom, top) if p1 <= p2 else (top, bottom)
    return [x, (q1 - f) / d, w, (q2 - q1) / d]


def _taller(page: pymupdf.Page, rp: Replay, cells: list[Cell], across, dy: float) -> dict:
    """The merged cells' rectangles to get taller by dy (shorter when negative), their top
    staying: those inside a cell that `across` says run across the spot (PDF y0, y1 ->
    bool). As moving.shift_below takes them."""
    from .columns import _rect_of

    out = {}
    if not cells:
        return out
    boxes = [to_pdf(page, c.bbox) for c in cells]
    for p in moving._pieces(rp):
        if p.kind not in ("path", "clip"):
            continue
        b = p.box
        box = next((c for c in boxes if b[0] >= c[0] - THIN and b[2] <= c[2] + THIN and b[1] >= c[1] - THIN and b[3] <= c[3] + THIN), None)
        if box is None:
            continue
        if b[3] - b[1] <= THIN or not across(b[1], b[3]):
            continue  # a line along a row: it moves with its row, or stays
        if b[1] > box[1] + THIN or b[3] < box[3] - THIN:
            continue  # not the whole cell's: a strip behind a line of its text goes with the text (_strips)
        if abs(p.ctm[1]) > 1e-9 or abs(p.ctm[2]) > 1e-9:
            raise EditRejected("A merged cell here is drawn in a way that can't be made taller yet.")
        found = _rect_of(rp, p)
        if found is None:
            raise EditRejected("A merged cell here is drawn in a way that can't be made taller yet.")
        re_at, operands = found
        new = (b[0], b[1] - dy, b[2], b[3])
        out[p.start] = (re_at, p.index, _re_rows(p.ctm, operands, new[1], new[3]), new)
    return out


def _sits(table: Table, cell: Cell) -> str | None:
    """Where a merged cell's text sits in it: "top", "middle" or "bottom"; None without text.
    The table's other cells say how far from the top text usually starts."""
    if not cell.lines:
        return None
    top = min(l.bbox[1] for l in cell.lines) - cell.bbox[1]
    bottom = cell.bbox[3] - max(l.bbox[3] for l in cell.lines)
    if abs(top - bottom) <= 1.5:
        return "middle"
    gaps = sorted(min(l.bbox[1] for l in c.lines) - c.bbox[1] for c in table.cells if c.rowspan == 1 and c.lines)
    pad = gaps[len(gaps) // 2] if gaps else min(top, bottom)
    if top <= pad + 1.5:
        return "top"
    if bottom <= pad + 1.5:
        return "bottom"
    if abs(top - bottom) <= 2.5:
        return "middle"
    raise EditRejected("The text of a merged cell here sits neither at its top, its middle nor its bottom, so rows can't be changed beside it yet.")


def _inside(page: pymupdf.Page, rp: Replay, cell: Cell, grown: float = 0.0) -> tuple[list, list, tuple]:
    """A merged cell's own pieces (paths and clips drawn inside it, as wide as it), the
    paints of those filling all of it, and its box (PDF; `grown` points taller now)."""
    x0, y0, x1, y1 = to_pdf(page, cell.bbox)
    y0 -= grown
    wide = lambda b: b[0] >= x0 - THIN and b[2] <= x1 + THIN and b[0] <= x0 + THIN and b[2] >= x1 - THIN
    own = [p for p in moving._pieces(rp) if p.kind in ("path", "clip") and wide(p.box) and p.box[1] >= y0 - THIN and p.box[3] <= y1 + THIN and p.box[3] - p.box[1] > THIN]
    whole = [rp.paths[p.index].fill for p in own if p.kind == "path" and rp.paths[p.index].fills and p.box[1] <= y0 + THIN and p.box[3] >= y1 - THIN]
    return own, whole, (x0, y0, x1, y1)


def _drawn_as_one(page: pymupdf.Page, rp: Replay, cell: Cell) -> None:
    """Refuse a cell that only reads as merged: two cells without a border between them,
    each with its own clipping or shading (Word clips a merged cell's text to all of it)."""
    own, whole, (x0, y0, x1, y1) = _inside(page, rp, cell)
    for p in own:
        if p.box[1] <= y0 + THIN and p.box[3] >= y1 - THIN:
            continue
        path = rp.paths[p.index]
        if p.kind == "clip" or (path.fills and path.fill not in whole):
            raise EditRejected("A cell here looks merged over several rows but is drawn as separate cells without a border between them, so rows can't be added or deleted inside it yet.")


def _strips(page: pymupdf.Page, rp: Replay, cell: Cell, lines: list, now: float = 0.0, grown: float = 0.0) -> list:
    """What's drawn behind a merged cell's lines (as displayed, `now` points lower than in
    `lines`): Word shades a shaded cell's paragraphs too, a strip a line tall behind each,
    painted as the whole cell is; and the lines' underlines and highlights. They go with
    the text. The cell is `grown` points taller now."""
    own, whole, _box = _inside(page, rp, cell, grown)
    around = []
    for l in lines:
        b = to_pdf(page, (l.bbox[0], l.bbox[1] + now, l.bbox[2], l.bbox[3] + now))
        reach = 0.6 * l.style.size
        around.append((b[1] - reach, b[3] + reach))
    out = [p for p in own if p.kind == "path" and rp.paths[p.index].fills and rp.paths[p.index].fill in whole and any(p.box[1] >= lo and p.box[3] <= hi for lo, hi in around)]
    found = page_lines(page)
    marks = set()
    for l in lines:
        n = next((n for n in found if comparable(n.text) == comparable(l.text) and abs(n.bbox[0] - l.bbox[0]) < 1 and abs(n.bbox[1] - l.bbox[1] - now) < 1), None)
        if n is not None:
            marks |= _skipped(rp, [n])
    out += [p for p in moving._pieces(rp) if p.kind == "path" and p.index in marks and p not in out]
    return out


def _held(src: bytes, page_no: int, cells: list[Cell], across, dy: float) -> frozenset:
    """What of merged cells stays put while the rows below move (by first instruction):
    their lines' text objects and what's drawn behind them, which then go where they sit
    in their cell (_keep_place); and their own shading in strips (Word shades each
    paragraph of a shaded cell, empty ones too) that `across` says runs across the spot,
    painted as the whole cell is: unseen on it, it stays, but must stay inside the cell
    (`dy` taller; shorter when negative)."""
    if not cells:
        return frozenset()
    doc, page, pdf, rp = _open(src, page_no)
    starts = set()
    pieces = moving._pieces(rp)
    # Their text, by where its letters are (the page may read otherwise in between: a row's
    # borders gone before the rows below close up).
    boxes = [to_pdf(page, l.bbox) for c in cells for l in c.lines]
    for p in pieces:
        if p.kind != "text":
            continue
        letters = [moving._glyph_box(rp.glyphs[t]) for t in p.glyphs if not moving._blank(rp, rp.glyphs[t])]
        ins = [any(b[0] - 1 <= (g[0] + g[2]) / 2 <= b[2] + 1 and b[1] - 1 <= (g[1] + g[3]) / 2 <= b[3] + 1 for b in boxes) for g in letters]
        if any(ins) and not all(ins):
            raise EditRejected("The text of a merged cell here is drawn together with other text, so it can't be kept in its place yet.")
        if letters and all(ins):
            starts.add(p.start)
    for c in cells:
        behind = _strips(page, rp, c, c.lines) if c.lines else []
        starts.update(p.start for p in behind)
        x0, y0, x1, y1 = to_pdf(page, c.bbox)
        inside = lambda b: b[0] >= x0 - THIN and b[2] <= x1 + THIN and b[1] >= y0 - THIN and b[3] <= y1 + THIN
        whole = [rp.paths[p.index].fill for p in pieces if p.kind == "path" and rp.paths[p.index].fills and inside(p.box) and p.box[1] <= y0 + THIN and p.box[3] >= y1 - THIN]
        for p in pieces:
            b = p.box
            if p.kind != "path" or p.start in starts or not inside(b) or b[3] - b[1] <= THIN or not across(b[1], b[3]):
                continue
            if b[1] <= y0 + THIN and b[3] >= y1 - THIN:
                continue  # the whole cell's: it gets taller (_taller)
            if rp.paths[p.index].fill not in whole or not rp.paths[p.index].fills or rp.paths[p.index].strokes:
                continue  # something else across the spot: the move refuses it
            if dy < 0 and b[1] < y0 - dy - 0.5:
                raise EditRejected("A merged cell's shading here wouldn't fit in it with this row gone.")
            starts.add(p.start)
    return frozenset(starts)


def _keep_place(src: bytes, page_no: int, table: Table, cells: list[Cell], dy: float) -> tuple[bytes, list]:
    """Each merged cell's text where it sits in the cell, now `dy` points taller (as
    displayed; shorter when negative); held still while the rows moved, it's where it was.
    Middle text goes half as far, top text stays, bottom text goes all the way. Returns the
    file and the areas that changed."""
    current, areas = src, []
    for cell in cells:
        where = _sits(table, cell)
        if where is None:
            continue
        want = {"top": 0.0, "middle": dy / 2, "bottom": dy}[where]
        if abs(want) < 0.01:
            continue
        current, changed = _move_text(current, page_no, cell, want, dy)
        areas += changed
    return current, areas


def _move_text(src: bytes, page_no: int, cell: Cell, dy: float, grown: float = 0.0) -> tuple[bytes, list]:
    """A merged cell's lines (where they were), with what's drawn behind them, moved dy
    points lower, whole, with their links; checked as other moves are (but for how the page
    reads, which the row change checks at its end: _reads_same). The cell is `grown`
    points taller now."""
    from .changes import LinkChange, page_links

    doc, page, pdf, rp = _open(src, page_no)
    # Its text, by where its letters are.
    boxes = [to_pdf(page, l.bbox) for l in cell.lines]
    pieces = []
    for p in moving._pieces(rp):
        if p.kind != "text":
            continue
        letters = [moving._glyph_box(rp.glyphs[t]) for t in p.glyphs if not moving._blank(rp, rp.glyphs[t])]
        ins = [any(b[0] - 1 <= (g[0] + g[2]) / 2 <= b[2] + 1 and b[1] - 1 <= (g[1] + g[3]) / 2 <= b[3] + 1 for b in boxes) for g in letters]
        if any(ins) and not all(ins):
            raise EditRejected("The text of a merged cell here is drawn together with other text, so it can't be kept in its place yet.")
        if letters and all(ins):
            pieces.append(p)
    if not pieces:
        raise EditRejected("The table couldn't be read back after making room. Nothing was changed.")
    pieces = sorted(pieces + _strips(page, rp, cell, cell.lines, 0.0, grown), key=lambda p: p.start)
    a = to_pdf(page, (0, 0, 0, 0))
    b = to_pdf(page, (0, dy, 0, dy))
    pdy = a[1] - b[1]
    glyphs = {t for p in pieces for t in p.glyphs}
    links = []
    for link in page_links(pdf, page_no, rp):
        under = link.glyphs & glyphs
        if under and under != link.glyphs:
            raise EditRejected("A link covers a merged cell's text and other text, so it can't be kept in its place yet.")
        if under:
            x0, y0, x1, y1 = link.rect
            links.append(LinkChange(link, (x0, y0 - pdy, x1, y1 - pdy)))
    box = moving._union(p.box for p in pieces)
    shift = moving.Shift(page_no, pdy, pieces, links, box, [], saved=True, held=frozenset(glyphs))
    moving.apply(pdf, page_no, rp, shift)
    out = io.BytesIO()
    pdf.save(out)
    new = out.getvalue()
    areas = moving.verify(src, new, page_no, rp, shift, page_lines(page))
    return new, areas


def table_json(page: pymupdf.Page, lines: list[Line]) -> list[dict]:
    """The page's tables with borders, for the website: each row's cells in the table's
    order, with their text and the ids of their lines."""
    out = []
    for table in find_tables(page, lines):
        if not table.ruled:
            continue
        rows = []
        for r in range(len(table.rows) - 1):
            spot = Spot(table, r)
            rows.append([
                {
                    "bbox": [round(v, 2) for v in c.bbox],
                    # Where it is in the table's grid (column 0: the leftmost on the page), and
                    # how many rows and columns it covers when merged.
                    "row": c.row,
                    "col": c.col,
                    "rowspan": c.rowspan,
                    "colspan": c.colspan,
                    "text": " ".join(l.text for l in c.lines),
                    "lines": [l.id for l in c.lines],
                    "merged": c.rowspan > 1 or any(o.rowspan > 1 and o.row <= r < o.row + o.rowspan for o in table.cells),
                }
                for c in spot.cells
            ])
        # Its column edges (left to right) and how far each can be dragged (columns.py); how
        # much smaller or bigger the whole table can get (scaling.py).
        from .columns import border_limits
        from .scaling import scale_limits

        limits = border_limits(page, table, lines)
        scale = scale_limits(page, table, lines)
        out.append({
            "bbox": [round(v, 2) for v in table.bbox],
            "rtl": table.rtl,
            "rows": rows,
            "cols": [round(v, 2) for v in table.cols],
            "limits": [[round(a, 2), round(b, 2)] for a, b in limits],
            "scale": [round(scale[0], 4), round(scale[1], 4)],
        })
    return out


# Borders and shading -------------------------------------------------------------------------


def _row_paths(rp: Replay, band: tuple[float, float, float, float], skip: set[int] = frozenset(), holes: list = (), inner: list = ()) -> dict[int, list[int]]:
    """The row's own borders and shading, in a band (PDF: x0, y0 bottom, x1, y1 top): for
    each path, which of its subpaths. Every piece whose middle is in the row (its side
    borders, Word's corner pieces under its top line, its shading, however many strips
    it's drawn in) and the line along its bottom edge; not the line along its top edge
    (the row above's), nor pieces running into other rows, nor the paths in `skip`
    (underlines and highlights of its text, which go with the text), nor anything inside
    `holes` (PDF x0, x1: merged cells the copy goes inside, which get taller instead), nor
    shading inside `inner` (merged cells over this row and others: their shading is
    drawn for all their rows, or behind their text)."""
    x0, y0, x1, y1 = band
    out: dict[int, list[int]] = {}
    for i, p in enumerate(rp.paths):
        if p.clip or not (p.fills or p.strokes) or i in skip:
            continue
        picked = []
        for k, sp in enumerate(p.subpaths):
            a, b, c, d = sp.bbox
            if c < x0 - THIN or a > x1 + THIN:
                continue  # beside the table
            mid = (b + d) / 2
            thin = d - b <= THIN
            if thin and abs(mid - y1) <= EDGE:
                continue  # the line along its top edge
            if any(h0 + THIN < (a + c) / 2 < h1 - THIN for h0, h1 in holes):
                continue  # inside a merged cell the new row goes into: nothing of its own there
            if not thin and c - a > THIN and any(h0 + THIN < (a + c) / 2 < h1 - THIN for h0, h1 in inner):
                continue  # a merged cell's shading
            if (thin and abs(mid - y0) <= EDGE) or (y0 < mid < y1 and b >= y0 - THIN and d <= y1 + THIN):
                picked.append(k)
        if picked:
            out[i] = picked
    return out


def _skipped(rp: Replay, lines: list[Line]) -> set[int]:
    """The underlines, strikethroughs and highlights of these lines' text."""
    from . import decorations

    return {d.path for l in lines for d in decorations.for_line(l, rp)}


def _cut_under(page: pymupdf.Page, rp: Replay, edge: float, x0: float, x1: float) -> float:
    """Where to cut at a row edge (PDF y): just under the border line drawn along it, so
    the line stays and everything under it moves (Word draws, under each row's line,
    short corner pieces that belong to the row below)."""
    bottom = edge
    for p in rp.paths:
        if p.clip or not (p.fills or p.strokes):
            continue
        half = p.line_width / 2 if p.strokes else 0.0
        for sp in p.subpaths:
            b = to_display(page, sp.bbox)
            if b.height <= THIN and b.width > THIN and b.x1 > x0 and b.x0 < x1 and abs((b.y0 + b.y1) / 2 - edge) <= EDGE:
                bottom = max(bottom, b.y1 + half)
    return to_pdf(page, (0, bottom + 0.02, 1, bottom + 0.02))[1]


def _op(operands, op: str):
    return pikepdf.ContentStreamInstruction(operands, pikepdf.Operator(op))


def _translate(ctm, down: float) -> list[float]:
    """A cm that moves what follows `down` points lower on the page (up when negative)."""
    m = mul(mul(ctm, (1, 0, 0, 1, 0, -down)), inverse(ctm))
    return [1, 0, 0, 1, round(m[4], 6), round(m[5], 6)]


def _path_ops(rp: Replay, i: int, subs: list[int]) -> list:
    """A path's instructions, with only some of its subpaths."""
    p = rp.paths[i]
    if len(subs) == len(p.subpaths):
        return list(rp.instructions[p.start : p.end + 1])
    ops = []
    for k in subs:
        sp = p.subpaths[k]
        ops += rp.instructions[sp.first : sp.last + 1]
    return ops + [rp.instructions[p.end]]


def _copy_row(src: bytes, page_no: int, band: tuple, down: float, holes: list = (), edge: tuple | None = None, inner: list = ()) -> tuple[bytes, dict[int, list[int]]]:
    """The row's borders and shading in `band` drawn again `down` points lower (higher
    when negative), each right after the original, in the same drawing state. With `edge`
    (PDF y, x0, x1), only the line along that edge between x0 and x1 instead."""
    doc, page, pdf, rp = _open(src, page_no)
    shown = to_display(page, band)
    in_row = [l for l in page_lines(page) if shown.y0 < (l.bbox[1] + l.bbox[3]) / 2 < shown.y1]
    picked = _edge_paths(rp, *edge) if edge else _row_paths(rp, band, _skipped(rp, in_row), holes, inner)
    if not picked:
        raise EditRejected("This table's borders can't be found, so a row can't be added to it yet.")
    reach = _reach_up(rp, picked, down, holes)
    after = {}
    for i, subs in picked.items():
        ops = _path_ops(rp, i, subs)
        if i in reach:
            # Its top drawn up to the border above (see _reach_up).
            sp = rp.paths[i].subpaths[subs[0]]
            ops = [_op(_re_rows(rp.paths[i].ctm, [float(v) for v in rp.instructions[sp.first].operands], sp.bbox[1], sp.bbox[3] + reach[i]), "re"), rp.instructions[rp.paths[i].end]]
        after[rp.paths[i].end] = [_op([], "q"), _op(_translate(rp.paths[i].ctm, down), "cm"), *ops, _op([], "Q")]
    out = []
    for k, ins in enumerate(rp.instructions):
        out.append(ins)
        out += after.get(k, [])
    write_page(pdf, page_no, out)
    buf = io.BytesIO()
    pdf.save(buf)
    new = buf.getvalue()
    _verify_copy(src, new, page_no, rp, picked, band, down, reach)
    return new, picked


def _reach_up(rp: Replay, picked: dict, down: float, holes: list) -> dict:
    """The copied side borders of merged cells that the new row goes inside, whose top
    would stop short of the border above: path -> how much higher to draw it. Chrome gives
    the little square where two rows' borders meet to the row below's side border, and
    inside a merged cell no line along the row covers it; a gap of a hair would show."""
    out = {}
    if not holes:
        return out
    for i, subs in picked.items():
        path = rp.paths[i]
        if len(subs) != 1 or len(path.subpaths) != 1 or abs(path.ctm[1]) > 1e-9 or abs(path.ctm[2]) > 1e-9:
            continue
        sp = path.subpaths[subs[0]]
        a, b, c, d = sp.bbox
        if c - a > THIN or d - b <= THIN or sp.first != sp.last or str(rp.instructions[sp.first].operator) != "re":
            continue
        x = (a + c) / 2
        if not any(abs(x - h) <= THIN for h0, h1 in holes for h in (h0, h1)):
            continue
        top = d - down
        above = [o.bbox[1] for q in rp.paths for o in q.subpaths if abs((o.bbox[0] + o.bbox[2]) / 2 - x) < 0.1 and o.bbox[2] - o.bbox[0] <= THIN and top < o.bbox[1] <= top + 1.0]
        if above:
            out[i] = min(above) - top
    return out


def _edge_paths(rp: Replay, y: float, x0: float, x1: float) -> dict[int, list[int]]:
    """The line along a row edge (PDF y) between x0 and x1: for each path, which of its
    subpaths (thin ones on the edge whose middle is between them, or at their ends)."""
    out: dict[int, list[int]] = {}
    for i, p in enumerate(rp.paths):
        if p.clip or not (p.fills or p.strokes):
            continue
        picked = [k for k, sp in enumerate(p.subpaths) if sp.bbox[3] - sp.bbox[1] <= THIN and abs((sp.bbox[1] + sp.bbox[3]) / 2 - y) <= EDGE and x0 - THIN <= (sp.bbox[0] + sp.bbox[2]) / 2 <= x1 + THIN]
        if picked:
            out[i] = picked
    return out


def _verify_copy(old: bytes, new: bytes, page_no: int, rp: Replay, picked: dict[int, list[int]], band: tuple, down: float, reach: dict | None = None) -> None:
    """Nothing changed but the new row: the same text, every shape as it was with the
    copies right after their originals, nothing outside the new row's place different
    by a pixel; and the copies look exactly like the row's own borders and shading."""
    old_doc, new_doc = pymupdf.open("pdf", old), pymupdf.open("pdf", new)
    old_page, new_page = old_doc[page_no], new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    if [(g.code, g.origin) for g in rp.glyphs] != [(g.code, g.origin) for g in new_rp.glyphs]:
        fail("the text on the page would have changed")
    expected = []
    for i, p in enumerate(rp.paths):
        expected.append((p.op, p.bbox))
        if i in picked:
            boxes = [p.subpaths[k].bbox for k in picked[i]]
            x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            expected.append((p.op, (x0, y0 - down, x1, y1 - down + (reach or {}).get(i, 0.0))))
    got = [(p.op, p.bbox) for p in new_rp.paths]
    if len(got) != len(expected) or any(a[0] != b[0] or any(abs(u - v) > 0.01 for u, v in zip(a[1], b[1])) for a, b in zip(expected, got)):
        fail("the table's lines would have been drawn wrongly")
    if [im.bbox for im in rp.images] != [im.bbox for im in new_rp.images]:
        fail("the page's pictures would have changed")
    x0, y0, x1, y1 = band
    new_band = to_display(old_page, (x0 - 2, y0 - down - 2, x1 + 2, y1 - down + 2))
    old_mu, old_pf = render(old, page_no)
    new_mu, new_pf = render(new, page_no)
    if outside(old_mu, new_mu, [tuple(new_band)], 2.0, 0) or outside(old_pf, new_pf, [tuple(new_band)], 2.0, 1):
        fail("other parts of the page would have changed")
    # The copies alone against the row's own pieces alone, at a zoom that moves them by
    # whole pixels. The row's top border isn't copied: its edge is left out.
    old_pieces = moving._pieces(rp)
    new_pieces = moving._pieces(new_rp)
    keep_old = {n for n, p in enumerate(old_pieces) if p.kind == "path" and p.index in picked}
    copies = {k for k, (a, b) in enumerate(zip(expected, got))} - _original_positions(rp, picked)
    keep_new = {n for n, p in enumerate(new_pieces) if p.kind == "path" and p.index in copies}
    length = abs(down)
    zoom = max(1, round(2 * length)) / length
    shift = round(down * zoom)
    area = to_display(old_page, (x0 - 1, y0 - 1, x1 + 1, y1 - THIN))
    old_mu, old_pf = render(moving._only(old, page_no, keep_old), page_no, zoom)
    new_mu, new_pf = render(moving._only(new, page_no, keep_new), page_no, zoom)
    for before, after, slack in ((old_mu, new_mu, 0), (old_pf, new_pf, 1)):
        if moving._differs_after_shift(before, after, area, zoom, 0, shift, slack):
            fail("the new row wouldn't look like the row it copies")
    check_other_pages(old, new, {page_no})


def _original_positions(rp: Replay, picked: dict[int, list[int]]) -> set[int]:
    """Where the original paths sit in the new list, with the copies after them."""
    out, n = set(), 0
    for i in range(len(rp.paths)):
        out.add(n)
        n += 2 if i in picked else 1
    return out


def _remove_row(src: bytes, page_no: int, band: tuple, lift: list = ()) -> bytes:
    """The row's borders and shading taken away (its top border stays: the row above's).
    Under the last row of a merged cell (`lift`: its PDF x0, x1), the line along the row's
    bottom goes up to its top instead: the merged cell's bottom border, and the next row's
    top one."""
    doc, page, pdf, rp = _open(src, page_no)
    picked = _row_paths(rp, band)
    x0, y0, x1, y1 = band
    pictures = {im.op for im in rp.images if y0 < (im.bbox[1] + im.bbox[3]) / 2 < y1 and x0 < (im.bbox[0] + im.bbox[2]) / 2 < x1}
    drop: set[int] = set(pictures)
    raised: dict[int, list] = {}
    for i, subs in picked.items():
        p = rp.paths[i]
        up = [k for k in subs if _under(p.subpaths[k].bbox, y0, lift)]
        if up:
            if abs(p.ctm[1]) > 1e-9 or abs(p.ctm[2]) > 1e-9:
                raise EditRejected("The border under a merged cell here is drawn in a way that can't be moved yet.")
            for k in up:
                sp = p.subpaths[k]
                ins = rp.instructions[sp.first]
                if sp.first != sp.last or str(ins.operator) != "re":
                    raise EditRejected("The border under a merged cell here is drawn in a way that can't be moved yet.")
                b = sp.bbox
                raised[sp.first] = _re_rows(p.ctm, [float(v) for v in ins.operands], b[1] + (y1 - y0), b[3] + (y1 - y0))
            subs = [k for k in subs if k not in up]
            if not subs:
                continue
        if len(subs) == len(p.subpaths):
            drop.update(range(p.start, p.end + 1))
        else:
            for k in subs:
                sp = p.subpaths[k]
                drop.update(range(sp.first, sp.last + 1))
    write_page(pdf, page_no, [_op(raised[k], "re") if k in raised else ins for k, ins in enumerate(rp.instructions) if k not in drop])
    buf = io.BytesIO()
    pdf.save(buf)
    new = buf.getvalue()
    # Nothing else changed: the same text, the other shapes as they were, and no pixel
    # outside the row's place.
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    if [(g.code, g.origin) for g in rp.glyphs] != [(g.code, g.origin) for g in new_rp.glyphs]:
        fail("the text on the page would have changed")
    if [im.bbox for im in rp.images if im.op not in pictures] != [im.bbox for im in new_rp.images]:
        fail("the page's pictures would have changed")
    gone = lambda i, p: i in picked and len(picked[i]) == len(p.subpaths) and not any(_under(p.subpaths[k].bbox, y0, lift) for k in picked[i])
    kept = [p for i, p in enumerate(rp.paths) if not gone(i, p)]
    if len(kept) != len(new_rp.paths) or any(a.op != b.op for a, b in zip(kept, new_rp.paths)):
        fail("the table's lines would have been changed wrongly")
    # The lines raised: as they were, a row higher.
    h = y1 - y0
    want = sorted(tuple(round(v, 2) for v in (b[0], b[1] + h, b[2], b[3] + h)) for p in rp.paths for b in (sp.bbox for sp in p.subpaths) if _under(b, y0, lift))
    got = [tuple(round(v, 2) for v in sp.bbox) for p in new_rp.paths for sp in p.subpaths]
    if any(not any(all(abs(u - v) < 0.02 for u, v in zip(w, g)) for g in got) for w in want):
        fail("the border under a merged cell wouldn't be where it should")
    page = doc[page_no]
    # Its top border stays, every piece of it (Word's corner pieces under it go: they're
    # the row's own, and they touch the line, so the line's pixels may change a little).
    top = [sp.bbox for p in rp.paths for sp in p.subpaths if (p.fills or p.strokes) and not p.clip and sp.bbox[3] - sp.bbox[1] <= THIN and abs((sp.bbox[1] + sp.bbox[3]) / 2 - y1) <= EDGE and sp.bbox[2] > x0 and sp.bbox[0] < x1]
    after = [sp.bbox for p in new_rp.paths for sp in p.subpaths]
    if any(not any(all(abs(u - v) < 0.01 for u, v in zip(b, a)) for a in after) for b in top):
        fail("the border above the row would have gone")
    area = to_display(page, (x0 - 2, y0 - 2, x1 + 2, y1 + THIN))
    old_mu, old_pf = render(src, page_no)
    new_mu, new_pf = render(new, page_no)
    if outside(old_mu, new_mu, [tuple(area)], 2.0, 0) or outside(old_pf, new_pf, [tuple(area)], 2.0, 1):
        fail("other parts of the page would have changed")
    return new


def _under(b: tuple, y0: float, lift: list) -> bool:
    """Is a piece (PDF box) the line along a row's bottom edge (y0) inside one of `lift`'s
    x ranges?"""
    return b[3] - b[1] <= THIN and abs((b[1] + b[3]) / 2 - y0) <= EDGE and any(h0 + THIN < (b[0] + b[2]) / 2 < h1 - THIN for h0, h1 in lift)


# The text in a new row ----------------------------------------------------------------------


@dataclass
class Placing:
    line: Line  # whose style the text takes
    x: float  # where it starts, ends or is centered (as displayed)
    y: float  # its middle
    align: str


def _placing(table: Table, row: int, cell: Cell, new_top: float, skip: list = ()) -> Placing | None:
    """Where a new cell's text goes and in which style: as the cell's own text sits, or
    the nearest cell with text in its column (in its own row, the nearest cell); not from
    the cells in `skip`. From a cell of another height (merged over other rows), in the
    middle of the new cell."""
    usable = [c for c in table.cells if c.lines and all(c is not k for k in skip)]
    same_col = sorted((c for c in usable if c.col == cell.col and c.colspan == cell.colspan), key=lambda c: abs(c.row - row))
    in_row = sorted((c for c in usable if c.row == row), key=lambda c: abs(c.col - cell.col))
    source = next(iter(same_col), None) or next(iter(in_row), None)
    if source is None:
        return None
    line = source.lines[0]
    lx0, ly0, lx1, ly1 = line.bbox
    sx0, sy0, sx1, sy1 = source.bbox
    left, right = lx0 - sx0, sx1 - lx1
    cx0, cy0, cx1, cy1 = cell.bbox
    y = new_top + ((ly0 + ly1) / 2 - sy0)
    if source.rowspan != cell.rowspan:
        y = (cy0 + cy1) / 2
    if abs(left - right) <= max(2.0, 0.1 * (sx1 - sx0)) and left > 2:
        return Placing(line, (cx0 + cx1) / 2, y, "center")
    # Against the side the cell's text keeps to, with the same margin, whichever way the
    # new text runs.
    return Placing(line, cx0 + left, y, "left") if left <= right else Placing(line, cx1 - right, y, "right")


def _find(lines: list[Line], text: str, bbox, dy: float) -> Line:
    """A line again after a step: by its text, where it was or `dy` lower."""
    for l in lines:
        if comparable(l.text) == comparable(text) and abs(l.bbox[0] - bbox[0]) < 1 and min(abs(l.bbox[1] - bbox[1]), abs(l.bbox[1] - bbox[1] - dy)) < 1:
            return l
    raise EditRejected("The table couldn't be read back after making room. Nothing was changed.")


# Adding a row --------------------------------------------------------------------------------


def _room_refused(reason: str) -> EditRejected:
    if "drawn across" in reason:
        return EditRejected("This table's lines run through all its rows, so a row can't be added to it yet.")
    if "bottom of the page" in reason or "footer" in reason or "border below" in reason:
        return EditRejected("There's no room on this page for another row: nothing moves onto the next page.")
    return EditRejected(f"A row can't be added here: {reason}.")


def add_row(src: bytes, page_no: int, line_id: str, old_text: str, where: str, texts: list[str]) -> EditResult:
    """A new row below or above the row a line is in, with `texts` in its cells (in the
    table's order; empty for an empty cell)."""
    if where not in ("below", "above"):
        raise EditRejected("A new row goes below or above a row.")
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    spot = locate(page, lines, line_id, old_text)
    table, picked = spot.table, spot.cell
    # Beside a cell merged over several rows: below its last row, or above its first. A
    # cell merged over the rows on both sides of the new one gets a row taller instead.
    row = picked.row + picked.rowspan - 1 if where == "below" else picked.row
    spot = Spot(table, row, picked)
    taller = _merged_across(table, row + 1 if where == "below" else row)
    cells = _row_cells(table, row, taller)
    texts = [t.strip() for t in texts][: len(cells)] + [""] * max(0, len(cells) - len(texts))
    height = spot.bottom - spot.top
    tx0, tx1 = table.cols[0], table.cols[-1]
    new_top = spot.bottom if where == "below" else spot.top  # as displayed
    band = to_pdf(page, (tx0, spot.top, tx1, spot.bottom))  # the row copied, before anything moves
    cut = _cut_under(page, rp, new_top, tx0, tx1)
    edge = to_pdf(page, (0, new_top, 1, new_top))[1]
    crop = moving._crop(pdf, page_no)
    sizes = sorted(l.style.size for c in cells for l in c.lines) or [10.0]
    size = sizes[len(sizes) // 2]

    # 1. Room: everything below the spot, across the page, one row lower. The border at the
    #    spot stays (a little below it is the cut: a border's own thickness stays above it).
    areas = []
    crosses = lambda y0, y1: y0 < cut - 0.5 and y1 > cut + 0.5
    for c in taller:
        _drawn_as_one(page, rp, c)
    stretch = _taller(page, rp, taller, crosses, height)
    try:
        made = moving.shift_below(src, page_no, band, size, cut, height, column=(crop[0], crop[2]), stretch=stretch, hold=_held(src, page_no, taller, crosses, height))
    except moving.MoveRefused as e:
        raise _room_refused(str(e))
    if made is None:
        limit, _ = moving.bottom_limit(doc, page_no, lines, crop)
        if edge - height < limit - 0.01:
            raise EditRejected("There's no room on this page for another row: nothing moves onto the next page.")
        current = src
    else:
        current, moved_areas, _, _ = made
        areas += moved_areas

    # 2. The row's borders and shading, copied into the room. Added below: the row stays and
    #    the copy goes one row lower. Added above: the row itself moved down, and the copy
    #    goes where it was.
    if where == "below":
        copy_band, down = band, height
    else:
        copy_band, down = (band[0], band[1] - height, band[2], band[3] - height), -height
    holes = [(b[0], b[2]) for b in (to_pdf(page, c.bbox) for c in taller)]
    inner = [(b[0], b[2]) for b in (to_pdf(page, c.bbox) for c in cells if c.rowspan > 1)]
    current, _ = _copy_row(current, page_no, copy_band, down, holes, inner=inner)
    if where == "above":
        # Above a merged cell's first row: the new row's cell there gets a bottom border, its
        # top one drawn again (the row's own has none: it's inside the merged cell).
        for c in cells:
            if c.row == row and c.rowspan > 1:
                x0, _, x1, _ = to_pdf(page, c.bbox)
                current, _ = _copy_row(current, page_no, (x0, edge - THIN, x1, edge + THIN), height, edge=(edge, x0, x1))
    new_box = (tx0, new_top, tx1, new_top + height)
    areas.append((page_no, tx0 - 2, new_top - 2, tx1 + 2, new_top + height + 2))
    # The merged cells' text where it sits in them (with the new row's borders drawn: until
    # then, two cells' lines side by side can read as one).
    current, kept = _keep_place(current, page_no, table, taller, height)
    areas += kept

    # 3. The text of each cell.
    warnings, lookalikes = [], []
    for n, (cell, text) in enumerate(zip(cells, texts), start=1):
        if not text:
            continue
        shifted = Cell(cell.row, cell.col, 1, cell.colspan, (cell.bbox[0], new_top, cell.bbox[2], new_top + height))
        placing = _placing(table, spot.row, shifted, new_top, taller)
        if placing is None:
            raise EditRejected("This table has no text to take a style from.")
        moved_down = height if (placing.line.bbox[1] >= new_top - 0.01) else 0.0
        now = page_lines(pymupdf.open("pdf", current)[page_no])
        ref = _find(now, placing.line.text, placing.line.bbox, moved_down)
        try:
            result = add_line(current, page_no, text, ref.id, ref.text, at=(placing.x, placing.y), align=placing.align)
        except EditRejected as e:
            if "no room" in str(e).lower() or "too long" in str(e).lower():
                raise EditRejected(f"“{text}” doesn't fit in its cell (column {n}). Shorten it.")
            raise
        current = result.pdf
        warnings += [w for w in result.warnings if w not in warnings]
        lookalikes += [l for l in result.lookalikes if l not in lookalikes]
        areas += result.areas
    if taller:
        _reads_same(current, page_no, lines, [t for t in texts if t])
    return EditResult(current, warnings, " | ".join(t for t in texts if t), areas, [(page_no, *new_box)], lookalikes)


# Deleting a row ------------------------------------------------------------------------------


def delete_row(src: bytes, page_no: int, line_id: str, old_text: str) -> EditResult:
    """The row a line is in taken out of its table; what's below moves up."""
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    spot = locate(page, lines, line_id, old_text)
    table = spot.table
    if len(table.rows) - 1 <= 1:
        raise EditRejected("This is the table's only row. Delete its lines instead.")
    if spot.cell.rowspan > 1:
        raise EditRejected("This cell is merged over several rows. To delete a row, click a cell that's one row tall.")
    height = spot.bottom - spot.top
    tx0, tx1 = table.cols[0], table.cols[-1]
    band = to_pdf(page, (tx0, spot.top, tx1, spot.bottom))
    crop = moving._crop(pdf, page_no)
    # The row's own cells go; a cell merged over it and other rows gets a row shorter.
    own = [c for c in spot.cells if c.rowspan == 1]
    shorter = [c for c in table.cells if c.rowspan > 1 and c.row <= spot.row < c.row + c.rowspan]
    # A merged cell's text must still fit, with the room above and below it the table's
    # other cells have.
    single = [c for c in table.cells if c.rowspan == 1 and c.lines]
    above = sorted(min(l.bbox[1] for l in c.lines) - c.bbox[1] for c in single)
    below = sorted(c.bbox[3] - max(l.bbox[3] for l in c.lines) for c in single)
    room = (above[len(above) // 2] + below[len(below) // 2]) if single else 0.0
    for c in shorter:
        _drawn_as_one(page, rp, c)
        if c.lines and max(l.bbox[3] for l in c.lines) - min(l.bbox[1] for l in c.lines) + room > (c.bbox[3] - c.bbox[1]) - height + 0.5:
            raise EditRejected("A merged cell's text wouldn't fit in it with this row gone.")
        x0, _, x1, _ = to_pdf(page, c.bbox)
        if any(x0 < (im.bbox[0] + im.bbox[2]) / 2 < x1 and band[1] < (im.bbox[1] + im.bbox[3]) / 2 < band[3] for im in rp.images):
            raise EditRejected("A merged cell has a picture in this row, so the row can't be deleted yet.")
    sizes = sorted(l.style.size for c in own for l in c.lines) or [10.0]
    size = sizes[len(sizes) // 2]
    areas = [(page_no, tx0 - 2, spot.top - 2, tx1 + 2, spot.bottom + 2)]
    warnings: list[str] = []

    current = src
    row_lines = [l for c in own for l in c.lines]
    if row_lines:
        result = delete_lines(current, [(page_no, l.id, l.text) for l in row_lines], close_gap=False)
        current = result.pdf
        warnings += result.warnings
    # Under a merged cell's last row, the line along the row's bottom is its bottom border.
    lift = [(b[0], b[2]) for b in (to_pdf(page, c.bbox) for c in shorter if c.row + c.rowspan - 1 == spot.row)]
    current = _remove_row(current, page_no, band, lift)
    # Its bottom border is gone: everything from its bottom edge on moves up; the merged
    # cells over it get shorter.
    cut = to_pdf(page, (0, spot.bottom + 0.02, 1, spot.bottom + 0.02))[1]
    _, _, _, now = _open(current, page_no)
    over = lambda y0, y1: y0 < band[3] - 0.5 and y1 > band[1] + 0.5 and (y0 < band[1] - 0.5 or y1 > band[3] + 0.5)
    stretch = _taller(page, now, shorter, over, -height)
    try:
        made = moving.shift_below(current, page_no, band, size, cut, -height, column=(crop[0], crop[2]), stretch=stretch, hold=_held(current, page_no, shorter, over, -height))
    except moving.MoveRefused as e:
        raise EditRejected(f"The rows below couldn't be moved up: {e}. Nothing was changed.")
    if made is not None:
        current, moved_areas, _, _ = made
        areas += moved_areas
        current, kept = _keep_place(current, page_no, table, shorter, -height)
        areas += kept
    if shorter:
        gone = {l.id for l in row_lines}
        _reads_same(current, page_no, [l for l in lines if l.id not in gone], [])
    return EditResult(current, warnings, "", areas, [(page_no, tx0, spot.top, tx1, spot.top + 1)], [])


def _reads_same(src: bytes, page_no: int, lines: list[Line], typed: list[str]) -> None:
    """At the end of a row change beside merged cells (whose text was held still while rows
    moved, when the page may read otherwise for a moment): every line reads as it did, each
    on its own, its ends where they were along the row (each step checked exactly where
    every glyph went), and the typed text as new lines; nothing else."""
    found = page_lines(pymupdf.open("pdf", src)[page_no])
    left = list(found)
    for l in lines:
        n = next((n for n in left if comparable(n.text) == comparable(l.text) and abs(n.bbox[0] - l.bbox[0]) < 1 and abs(n.bbox[2] - l.bbox[2]) < 1), None)
        if n is None:
            fail("the table's text would read differently")
        left.remove(n)
    if sorted(comparable(n.text) for n in left) != sorted(comparable(t) for t in typed):
        fail("the table's text would read differently")
