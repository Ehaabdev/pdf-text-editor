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

Merged cells: a cell merged across the column gets a column narrower (its rectangles
shorter, its text moving with the side of the cell it keeps to, or half as far when it's
centered) and keeps its text; the column's own edge bars go only beside its own cells (a
merged cell ending at the column keeps its right border). A cell merged over several rows
is like any other: what matters is its columns. Clicking a cell merged across columns is
refused (which column?).

Checked before it's accepted: every glyph, shape and picture is where it was or exactly
one column's width to the left; MuPDF lists the same shapes, minus the column's, the
others in their new places; nothing outside the table changes by a pixel; the table's
part left of the column is pixel for pixel as it was, and its part right of the column
pixel for pixel as it was, one column's width further left (MuPDF and PDFium); every line
reads the same, the moved ones at their new place; and the page reads as a table with
one column fewer, the same rows, the same text in each cell. The text of merged cells
across the column is left out of the two-part comparison and compared on its own, each
line where it went; and when the column's cells were all that split two rows, those rows
read as one.

Refused, with the reason: a table without borders, a table's only column, a cell merged
across columns clicked, a merged cell too narrow for its text without the column, a turned
page, rectangles drawn some other way, or something drawn across the table's edge.
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
from .tables import Cell, Table, find_tables
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
    cell: Cell | None = None  # the cell picked (it may be merged across columns)


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
                return Spot(table, cell.col, cell)
    raise EditRejected("This line isn't in a table.")


def _text_dx(cell: Cell, line: Line, left_dx: list, right_dx: list) -> float:
    """How far a cell's line moves when its column edges do: with the edge of the side it
    keeps to, or halfway between them when it's centered. A merged cell goes from its first
    column's left edge to its last column's right edge."""
    a, b = left_dx[cell.col], right_dx[cell.col + cell.colspan - 1]
    side = _side(cell, line)
    return a if side == "left" else b if side == "right" else (a + b) / 2


def _pdf_rows(page: pymupdf.Page, table: Table, cell: Cell) -> tuple[float, float]:
    """A cell's rows, as PDF y: bottom, top."""
    y_top = to_pdf(page, (table.bbox[0], table.rows[cell.row], table.bbox[0], table.rows[cell.row]))[1]
    y_bottom = to_pdf(page, (table.bbox[0], table.rows[cell.row + cell.rowspan], table.bbox[0], table.rows[cell.row + cell.rowspan]))[1]
    return min(y_top, y_bottom), max(y_top, y_bottom)


def _beside(page: pymupdf.Page, table: Table, cells: list, box: tuple) -> list:
    """The cells (of `cells`) a rectangle is beside, by its rows: those it runs along for more
    than a border's width, or, for a dot, those whose rows it's at (above and below it)."""
    y0, y1 = box[1], box[3]
    out = []
    for c in cells:
        b, t = _pdf_rows(page, table, c)
        if y1 - y0 <= THIN:
            if b - THIN <= (y0 + y1) / 2 <= t + THIN:
                out.append(c)
        elif min(y1, t) - max(y0, b) > THIN:
            out.append(c)
    return out


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
    moves: list[tuple[Piece, float]]  # text and pictures moved, and how far right (PDF)
    boxes: dict[int, tuple]  # path index -> its box after, when it moved or got shorter
    dropped_paths: set[int]
    width: float  # the column's width (PDF)
    blanks: set[int]  # glyphs taken away with the column: spaces in its empty cells
    kept_state: dict[int, list]  # instruction -> what a dropped text object set, set again after it
    glyph_dx: dict[int, float]  # glyph -> how far it moves right (PDF)
    merged: set[int]  # glyphs of merged cells across the column: they move by how they sit in them


def _plan(page: pymupdf.Page, rp: Replay, table: Table, col: int) -> Plan:
    edges = _edges(page, table)
    k = col
    n = len(edges) - 1
    width = edges[k + 1] - edges[k]
    last = k == n - 1
    gone_edge = k if last else k + 1  # the edge whose bars go
    # Left of the column nothing moves; right of it everything moves a column's width.
    left_dx = [0.0 if i <= k else -width for i in range(n)]
    right_dx = [0.0 if i < k else -width for i in range(n)]
    x0, y0, x1, y1 = to_pdf(page, table.bbox)
    box = (x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
    inside = lambda b: b[0] >= box[0] and b[1] >= box[1] and b[2] <= box[2] and b[3] <= box[3]
    touches = lambda b: min(b[2], box[2]) > max(b[0], box[0]) and min(b[3], box[3]) > max(b[1], box[1])

    def shift_at(x: float) -> float:
        """How far a point moves: nothing left of the column, a column's width right of it."""
        return -width if x >= edges[k + 1] - THIN else 0.0

    def end_dx(x: float) -> float:
        """How far a rectangle's end moves: with the side of the column it's nearer to."""
        return 0.0 if x - edges[k] <= edges[k + 1] - x else -width

    # The column's cells; a merged one across it gets narrower, its text staying in it.
    slot = [c for c in table.cells if c.col <= k < c.col + c.colspan]
    merged = [c for c in slot if c.colspan > 1]
    cell_of = {l.id: c for c in merged for l in c.lines}
    line_of = {t: l for c in merged for l in c.lines for t in l.members}
    wd = _display_width(page, width)
    for c in merged:
        left, right = table.cols[c.col], table.cols[c.col + c.colspan] - wd
        for l in c.lines:
            d = _text_dx(c, l, left_dx, right_dx)
            dd = -_display_width(page, -d) if d else 0.0
            a, b = l.bbox[0] + dd, l.bbox[2] + dd
            if a - left < min(1.0, l.bbox[0] - c.bbox[0]) - 0.01 or right - b < min(1.0, c.bbox[2] - l.bbox[2]) - 0.01:
                raise EditRejected("A merged cell across this column would be too narrow for its text without it.")
    merged_boxes = [to_pdf(page, c.bbox) for c in merged]
    in_merged = lambda b: any(m[0] <= (b[0] + b[2]) / 2 <= m[2] and m[1] <= (b[1] + b[3]) / 2 <= m[3] for m in merged_boxes)

    pieces = moving._pieces(rp)
    drop, rects, moves, boxes, dropped_paths = set(), {}, [], {}, set()
    blanks: set[int] = set()
    kept_state: dict[int, list] = {}
    glyph_dx: dict[int, float] = {}

    def move(p: Piece, d: float) -> None:
        if d:
            moves.append((p, d))
            for t in p.glyphs:
                glyph_dx[t] = d

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
            # the row in the same text object. A merged cell's go by how they sit in it.
            ds: dict = {}
            for t in p.glyphs:
                l = line_of.get(t)
                if l is not None:
                    d = _text_dx(cell_of[l.id], l, left_dx, right_dx)
                    ds[round(d, 6)] = d
                elif not moving._blank(rp, rp.glyphs[t]):
                    b = moving._glyph_box(rp.glyphs[t])
                    x = (b[0] + b[2]) / 2
                    if edges[k] < x < edges[k + 1]:
                        ds["in"] = "in"
                    else:
                        d = shift_at(x)
                        ds[round(d, 6)] = d
            if ds:
                if len(ds) > 1:
                    raise EditRejected("This column's text is drawn together with its neighbours', so they can't be moved apart yet.")
                d = ds.popitem()[1]
                if d == "in":
                    raise EditRejected("This column's text couldn't be taken out exactly.")
                move(p, d)
                continue
        if p.kind in ("text", "image"):
            if edges[k] < cx < edges[k + 1]:
                if in_merged(p.box):
                    if p.kind == "image":
                        raise EditRejected("A merged cell across this column has a picture in it, so this column can't be deleted yet.")
                    continue  # the spaces of an empty merged cell: they stay
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
            move(p, shift_at(cx))
            continue
        found = _rect_of(rp, p)
        if found is None:
            raise EditRejected("This table's lines are drawn in a way that can't be changed yet.")
        re_at, operands = found
        a0, a1 = p.box[0], p.box[2]
        dot = p.box[3] - p.box[1] <= THIN
        j = min(range(n + 1), key=lambda e: abs(edges[e] - cx))
        on_edge = a1 - a0 <= THIN and abs(edges[j] - cx) <= THIN
        if a1 - a0 <= THIN and not on_edge and not dot:
            raise EditRejected("Some rows of this table are split into columns differently, so its columns can't be changed yet.")
        if on_edge:
            if j == gone_edge:
                # The column's own borders go; a merged cell ending at the column keeps
                # its right one (on the column's left edge, after); inside a merged cell
                # across the edge there's none.
                kinds = set()
                for c in _beside(page, table, slot, p.box):
                    kinds.add("own" if c.colspan == 1 else "end" if j == k + 1 and c.col + c.colspan - 1 == k else "inside")
                if dot:
                    keep = "end" in kinds
                elif kinds == {"own"}:
                    keep = False
                elif kinds == {"end"}:
                    keep = True
                else:
                    raise EditRejected("This table's borders are drawn in a way that can't be changed yet.")
                if not keep:
                    drop.update(range(p.start, p.end + 1))
                    dropped_paths.add(p.index)
                    continue
            d0 = d1 = 0.0 if j <= k else -width
        else:
            # A cell's shading or a bar along it (or a dot inside it): the column's own go;
            # one across the column (a merged cell's, or a line along the table) gets shorter.
            if edges[k] < cx < edges[k + 1] and a0 >= edges[k] - THIN and a1 <= edges[k + 1] + THIN:
                drop.update(range(p.start, p.end + 1))
                dropped_paths.add(p.index)
                continue
            d0, d1 = end_dx(a0), end_dx(a1)
        if d0 or d1:
            rects[re_at] = _re_operands(p.ctm, operands, a0 + d0, a1 + d1)
            boxes[p.index] = (a0 + d0, p.box[1], a1 + d1, p.box[3])
    return Plan(rp, drop, rects, moves, boxes, dropped_paths, width, blanks, kept_state, glyph_dx, set(line_of))


def _rewrite(pdf: pikepdf.Pdf, page_no: int, plan: Plan) -> None:
    rp = plan.rp
    before: dict[int, list] = {}
    after: dict[int, list] = {}
    for p, d in plan.moves:
        lasting = _lasting(rp, p)
        if lasting is None:
            raise EditRejected("This column's neighbours can't be moved exactly.")
        go, _back = moving._cm(p, 0.0, d)
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
    changes = []
    annots = pdf.pages[page_no].obj.get("/Annots")
    for link in page_links(pdf, page_no, rp):
        ds = {round(plan.glyph_dx.get(t, 0.0), 6) for t in link.glyphs}
        if ds <= {0.0}:
            continue
        if len(ds) > 1:
            raise EditRejected("A link covers text in two columns, so they can't be moved apart.")
        d = plan.glyph_dx[next(iter(link.glyphs))]
        a = annots[link.index]
        x0, y0, x1, y1 = link.rect
        a.Rect = pikepdf.Array([x0 + d, y0, x1 + d, y1])
        if "/QuadPoints" in a:
            q = [float(v) for v in a.QuadPoints]
            a.QuadPoints = pikepdf.Array([v + d if i % 2 == 0 else v for i, v in enumerate(q)])
        changes.append(LinkChange(link, (x0 + d, y0, x1 + d, y1)))
    return changes


def delete_column(src: bytes, page_no: int, line_id: str, old_text: str) -> EditResult:
    """The column a line is in, taken out of its table; the columns right of it move left."""
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    spot = locate(page, lines, line_id, old_text)
    table, col = spot.table, spot.col
    if len(table.cols) <= 2:
        raise EditRejected("This is the table's only column. Delete its lines instead.")
    if spot.cell.colspan > 1:
        raise EditRejected("This cell is merged across columns. To delete a column, click a cell that's one column wide.")

    # 1. Its text, with its underlines and links, as deleting lines does (checked there);
    # a merged cell across it keeps its text.
    column = [l for c in table.cells if c.col == col and c.colspan == 1 for l in c.lines]
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


def _text(cell: Cell) -> str:
    return comparable(" ".join(l.text for l in cell.lines))


def _structure(table: Table) -> list:
    """A table's cells as read: (row, column, rows, columns, text), in order."""
    return sorted((c.row, c.col, c.rowspan, c.colspan, _text(c)) for c in table.cells)


def _as_read(rows: list, cols: list, cells: list) -> tuple:
    """A table's row edges, column edges and cells (as _structure gives them) as the page
    reads them: an edge no cell starts or ends at doesn't show, so the cells on both sides
    of it read as one row (or column). Deleting the only column whose cells split a merged
    row in two does that."""
    rows, cols, cells = list(rows), list(cols), [list(c) for c in cells]
    for start, span, edges in ((0, 2, rows), (1, 3, cols)):
        j = 1
        while j < len(edges) - 1:
            if any(c[start] == j or c[start] + c[span] == j for c in cells):
                j += 1
                continue
            del edges[j]
            for c in cells:
                if c[start] > j:
                    c[start] -= 1
                elif c[start] < j < c[start] + c[span]:
                    c[span] -= 1
    return rows, cols, [tuple(c) for c in cells]


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

    # Every glyph where it was, or one column's width to the left (in a merged cell across
    # the column: as far as its side of the cell moved).
    remaining = [(t, g) for t, g in enumerate(rp.glyphs) if t not in plan.blanks]
    if len(new_rp.glyphs) != len(remaining):
        fail("the text on the page would have changed")
    for (t, a), b in zip(remaining, new_rp.glyphs):
        d = plan.glyph_dx.get(t, 0.0)
        if a.code != b.code or abs(a.origin[0] + d - b.origin[0]) > 0.01 or abs(a.origin[1] - b.origin[1]) > 0.01 or (a.paint.font, a.paint.size, a.paint.fill) != (b.paint.font, b.paint.size, b.paint.fill):
            fail("the text on the page would have changed")
    # Every shape kept is where it was, moved left, or shorter by the column; the column's are gone.
    kept = [(i, p) for i, p in enumerate(rp.paths) if i not in plan.dropped_paths]
    if len(kept) != len(new_rp.paths):
        fail("the table's lines would have changed wrongly")
    for (i, a), b in zip(kept, new_rp.paths):
        if a.op != b.op or a.clip != b.clip or not moving._close(b.bbox, plan.boxes.get(i, a.bbox), 0.01):
            fail("the table's lines would have changed wrongly")
    image_dx = {p.index: d for p, d in plan.moves if p.kind == "image"}
    dropped_images = {i for i, im in enumerate(rp.images) if im.op in plan.drop}
    kept_images = [(i, im) for i, im in enumerate(rp.images) if i not in dropped_images]
    if len(kept_images) != len(new_rp.images):
        fail("the page's pictures would have changed")
    for (i, a), b in zip(kept_images, new_rp.images):
        d = image_dx.get(i, 0.0)
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
    # The text of merged cells across the column moves otherwise: it's left out here and
    # compared on its own, below.
    edges = table.cols
    wd = _display_width(old_page, w)
    zoom = max(1, round(2 * wd)) / wd
    old_r, new_r = old, new
    if plan.merged:
        was = [t for t, _ in remaining]
        keep_old = {m for m, p in enumerate(moving._pieces(rp)) if not (p.kind == "text" and plan.merged & set(p.glyphs))}
        keep_new = {m for m, p in enumerate(moving._pieces(new_rp)) if not (p.kind == "text" and any(was[t] in plan.merged for t in p.glyphs))}
        old_r, new_r = moving._only(old, page_no, keep_old), moving._only(new, page_no, keep_new)
    # The table itself, not the text just above or below it (which stays put).
    x0, y0, x1, y1 = table.bbox
    left = pymupdf.Rect(x0 - 0.6, y0 - 0.6, edges[col] - THIN, y1 + 0.6)
    right = pymupdf.Rect(edges[col + 1] + THIN, y0 - 0.6, x1 + 0.6, y1 + 0.6)

    def differs(z: float, allow: int = 0) -> list[bool]:
        sx = -round(wd * z)
        old_mu, old_pf = render(old_r, page_no, z)
        new_mu, new_pf = render(new_r, page_no, z)
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

    # A line moves when its letters do (a space of it, in an empty cell further on, may too).
    letters = lambda l: {t for t in l.members if not moving._blank(rp, rp.glyphs[t])}

    def line_dx(l: Line) -> float:
        ds = {round(plan.glyph_dx.get(t, 0.0), 6) for t in letters(l)}
        d = ds.pop() if len(ds) == 1 else 0.0
        return -_display_width(old_page, -d) if d else 0.0

    # The merged cells' text: drawn alone, each line the same where it went (compared at a
    # size where its move is whole pixels).
    merged_lines = [l for l in lines if letters(l) and letters(l) <= plan.merged]
    if merged_lines:
        alone_old = moving._only(old, page_no, {m for m, p in enumerate(moving._pieces(rp)) if p.kind == "text"})
        alone_new = moving._only(new, page_no, {m for m, p in enumerate(moving._pieces(new_rp)) if p.kind == "text"})
        groups: dict = {}
        for l in merged_lines:
            groups.setdefault(line_dx(l), []).append(pymupdf.Rect(l.bbox) + (-1, -1, 1, 1))
        for d, rects in groups.items():

            def alone_differs(z: float, allow: int = 0) -> list[bool]:
                omu, opf = render(alone_old, page_no, z)
                nmu, npf = render(alone_new, page_no, z)
                return [any(moving._differs_after_shift(b, a, r, z, round(d * z), 0, slack, noise) for r in rects) for b, a, slack, noise in ((omu, nmu, 0, 0), (opf, npf, 1, allow))]

            z = max(1, round(2 * abs(d))) / abs(d) if d else 2.0
            first = alone_differs(z)
            if any(first):
                again = alone_differs(2 * z, allow=4 if not first[0] else 0)
                if any(a and b for a, b in zip(first, again)):
                    fail("the merged cells' text wouldn't look exactly the same")

    # Every line reads the same, the moved ones at their new place.
    expect = sorted(moving._line_key(l, (line_dx(l), 0.0)) for l in lines)
    got = sorted(moving._line_key(l, (0.0, 0.0)) for l in page_lines(new_page))
    if len(expect) != len(got) or any(e[0] != g[0] or not moving._close(e[1:], g[1:], 0.3) for e, g in zip(expect, got)):
        fail("the table's text would read differently")
    # The page reads as the table with one column fewer (when it has text left to read,
    # and more than one column: a single column doesn't read as a table): a merged cell
    # across the column one column narrower.
    gone = lambda c: c.colspan == 1 and c.col == col
    if not any(c.lines for c in table.cells if not gone(c)) or len(table.cols) <= 3:
        check_other_pages(old, new, {page_no})
        return tuple(tb)
    after = [t for t in find_tables(new_page, page_lines(new_page)) if t.ruled and abs(t.bbox[1] - table.bbox[1]) < 1]
    want_cols = [x for i, x in enumerate(edges) if i != (col + 1 if col < len(edges) - 2 else col)]
    want_cols = [x if x <= edges[col] + 0.5 else x - wd for x in want_cols]
    across = lambda c: c.col <= col < c.col + c.colspan
    want_cells = [(c.row, c.col if c.col <= col else c.col - 1, c.rowspan, c.colspan - across(c), _text(c)) for c in table.cells if not gone(c)]
    want_rows, want_cols, want_cells = _as_read(table.rows, want_cols, want_cells)
    if len(after) != 1 or len(after[0].cols) != len(want_cols) or not moving._close(after[0].cols, want_cols, 0.6) or len(after[0].rows) != len(want_rows) or not moving._close(after[0].rows, want_rows, 0.6):
        fail("the page wouldn't read as the same table with one column fewer")
    if sorted(want_cells) != _structure(after[0]):
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
#
# Merged cells: the new column gets a cell beside each cell of the column (beside a merged
# cell, its last column on the right, its first on the left), as tall as that cell, with its
# shading and bars; its edge gets the bars of the cell's inner side. A cell merged across
# the new column's place gets a column wider instead: its rectangles across it stretch, and
# what's drawn column by column in it gets its part in the new column too. A copy that a
# stretched rectangle (painted the same) already covers isn't drawn twice.


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
        if c.col != col or c.colspan > 1:
            continue
        widths += _room(c, margin)
    if not widths:
        return math.floor(max(0.0, (x1 - x0) - 2 * margin - MIN_WIDTH))
    return max(0, math.floor(min(widths)))


def _room(cell, margin: float) -> list:
    """How much narrower a cell can get for each of its lines, keeping its side and margin."""
    out = []
    for l in cell.lines:
        left, right = l.bbox[0] - cell.bbox[0], cell.bbox[2] - l.bbox[2]
        side = _side(cell, l)
        out.append(right - margin if side == "left" else left - margin if side == "right" else left + right - 2 * margin)
    return out


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
    # A merged cell's text must still fit: each of its columns gives up its share at most.
    for c in table.cells:
        room = _room(c, margin) if c.colspan > 1 else []
        if room:
            share = max(0, math.floor(min(room))) // c.colspan
            for i in range(c.col, c.col + c.colspan):
                spare[i] = min(spare[i], share)
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

    def mapped(a: float, L: float, R: float, nl: float, nr: float, right: bool = False) -> float:
        """A rectangle's end in a cell from L to R, in a new cell from nl to nr: as far from
        the side it's nearer to (its right end: the right side, when it's halfway)."""
        if right:
            return nr - (R - a) if R - a <= a - L else nl + (a - L)
        return nl + (a - L) if a - L <= R - a else nr - (R - a)

    # With merged cells, a new cell is added beside each cell of the column, like it (a
    # merged one across the new column's place gets wider instead), its borders as that
    # cell's: the new edge gets the bars of the cell's inner side.
    wide = any(c.colspan > 1 for c in table.cells)
    k, e = shape.model, shape.added
    slot = [c for c in table.cells if k is not None and c.col <= k < c.col + c.colspan]
    models = [c for c in slot if not (c.col < e < c.col + c.colspan)] if e is not None else []

    def model_edge(c) -> int:
        near, far = (c.col + c.colspan, c.col) if c.col + c.colspan == e else (c.col, c.col + c.colspan)
        return near if 0 < near < n else far if 0 < far < n else near

    def rows_of(c) -> tuple:
        return _pdf_rows(page, table, c)

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
        return _text_dx(c, l, left_dx, right_dx)

    pieces = moving._pieces(rp)
    rects, moves, glyph_dx, image_dx = {}, {}, {}, {}
    new_boxes: dict = {}
    copies: list = []  # (piece, its operands, new left, new right)
    grown: list = []  # merged cells' rectangles across the new column's place: (path, box after)
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
        dot = p.box[3] - p.box[1] <= THIN
        if p.kind == "clip":
            i = column_at((a0 + a1) / 2)
            across = a0 < E[i] - THIN or a1 > E[i + 1] + THIN
            if (a0 > E[i] + THIN or a1 < E[i + 1] - THIN) and not (wide and across):
                end = scopes.get(p.index, len(rp.instructions))
                inner = {moves.get(q.start, 0.0) for q in pieces if q.kind in ("text", "image") and p.end < q.start < end}
                if len(inner) > 1:
                    raise EditRejected("This table's cells are drawn in a way that can't be changed yet.")
                d = inner.pop() if inner else left_dx[i]
                if d:
                    rects[re_at] = _re_operands(p.ctm, operands, a0 + d, a1 + d)
                new_boxes[p.index] = (a0 + d, p.box[1], a1 + d, p.box[3])
                continue
        j = min(range(n + 1), key=lambda e_: abs(E[e_] - (a0 + a1) / 2))
        on_edge = a1 - a0 <= THIN and abs(E[j] - (a0 + a1) / 2) <= THIN
        if a1 - a0 <= THIN and not on_edge and not dot:
            raise EditRejected("Some rows of this table are split into columns differently, so its columns can't be changed yet.")
        if on_edge:
            d0 = d1 = edge_dx[j]
            if shape.missing_edge is not None and not (wide and p.kind == "clip"):
                # This edge's bars, drawn again on the new edge.
                if wide:
                    again = any(model_edge(c) == j for c in _beside(page, table, models, p.box))
                else:
                    again = shape.model_edge == j
                if again:
                    dd = E2[shape.missing_edge] - E[j]
                    copies.append((p, operands, a0 + dd, a1 + dd))
        else:
            # A cell's shading or a bar along it (or a dot inside it).
            i = column_at((a0 + a1) / 2)
            if a0 < E[i] - THIN or a1 > E[i + 1] + THIN:
                d0, d1 = end_dx(a0), end_dx(a1)  # across columns: each end with its own
                if wide and e is not None and a0 < E[e] < a1:
                    grown.append((p, (a0 + d0, p.box[1], a1 + d1, p.box[3])))
            else:
                d0 = left_dx[i] if a0 - E[i] <= E[i + 1] - a0 else right_dx[i]
                d1 = right_dx[i] if E[i + 1] - a1 <= a1 - E[i] else left_dx[i]
            if shape.added is not None and p.kind != "clip":
                nl, nr = E2[shape.added], E2[shape.added + 1]
                if not wide:
                    if i == k:
                        # The column it's added beside: its shading and bars, drawn again in the new column.
                        copies.append((p, operands, mapped(a0, E[i], E[i + 1], nl, nr), mapped(a1, E[i], E[i + 1], nl, nr, True)))
                elif i == k and a0 >= E[k] - THIN and a1 <= E[k + 1] + THIN:
                    # Within the column: drawn again in the new one (a merged cell drawn
                    # column by column gets its part in the new column too).
                    copies.append((p, operands, mapped(a0, E[k], E[k + 1], nl, nr), mapped(a1, E[k], E[k + 1], nl, nr, True)))
                else:
                    # A merged cell's own, across its columns: drawn again in its new cell.
                    for c in models:
                        L, R = E[c.col], E[c.col + c.colspan]
                        bottom, top = rows_of(c)
                        if c.colspan > 1 and L - THIN <= a0 and a1 <= R + THIN and a0 < E[k + 1] - THIN and a1 > E[k] + THIN and p.box[1] >= bottom - THIN and p.box[3] <= top + THIN:
                            copies.append((p, operands, mapped(a0, L, R, nl, nr), mapped(a1, L, R, nl, nr, True)))
                            break
        if d0 or d1:
            rects[re_at] = _re_operands(p.ctm, operands, a0 + d0, a1 + d1)
        new_boxes[p.index] = (a0 + d0, p.box[1], a1 + d1, p.box[3])

    # A copy drawn twice, or over a merged cell's own rectangle that now runs across the
    # new column, painted the same: once is enough.
    paint = lambda i: (rp.paths[i].op, rp.paths[i].fill, rp.paths[i].stroke)
    kept_copies, extra, after = [], {}, {}
    for p, operands, c0, c1 in copies:
        b = (c0, p.box[1], c1, p.box[3])
        covers = lambda o: o[0] <= b[0] + 0.01 and o[1] <= b[1] + 0.01 and o[2] >= b[2] - 0.01 and o[3] >= b[3] - 0.01
        if wide and (any(paint(q.index) == paint(p.index) and moving._close(o, b, 0.01) for q, o in kept_copies) or any(paint(q.index) == paint(p.index) and covers(o) for q, o in grown)):
            continue
        kept_copies.append((p, b))
        after.setdefault(p.end, []).append(_re_moved(rp, p, operands, c0, c1))
        extra.setdefault(p.index, []).append(b)

    path_boxes = []
    for i, path in enumerate(rp.paths):
        path_boxes.append(new_boxes.get(i, path.bbox))
        path_boxes += extra.get(i, [])
    nb = (min(shape.new[0], table.bbox[0]), table.bbox[1], max(shape.new[-1], table.bbox[2]), table.bbox[3])
    return ReshapePlan(rp, rects, after, moves, pieces, glyph_dx, path_boxes, image_dx, tuple(table.bbox), nb)


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
    # A merged cell across the new column's place is a column wider.
    after = [t for t in find_tables(new_page, page_lines(new_page)) if t.ruled and abs(t.bbox[1] - table.bbox[1]) < 1]
    e = shape.added
    grows = lambda c: e is not None and c.col < e < c.col + c.colspan
    want = [(c.row, shape.cols[c.col], c.rowspan, c.colspan + grows(c), _text(c)) for c in table.cells]
    if e is not None:
        want += [(c.row, e, c.rowspan, 1, "") for c in _new_cells(table, shape.model, e)]
    want_rows, want_cols, want = _as_read(table.rows, shape.new, want)
    if len(after) != 1 or len(after[0].cols) != len(want_cols) or not moving._close(after[0].cols, want_cols, 0.6) or len(after[0].rows) != len(want_rows) or not moving._close(after[0].rows, want_rows, 0.6):
        fail("the page wouldn't read as the same table with its new columns")
    if sorted(want) != _structure(after[0]):
        fail("the table's cells would read differently")
    check_other_pages(old, new, {page_no})
    return tuple(area)


def _new_cells(table: Table, col: int, added: int) -> list:
    """The cells of a column that a new column at edge `added` (beside it) gets a cell
    beside: all but those merged across that edge, which get wider instead."""
    return [c for c in table.cells if c.col <= col < c.col + c.colspan and not (c.col < added < c.col + c.colspan)]


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

    if where not in ("left", "right"):
        raise EditRejected("A new column goes left or right of a column.")
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    spot = locate(page, lines, line_id, old_text)
    table, cell = spot.table, spot.cell
    rows = len(table.rows) - 1
    texts = [t.strip() for t in texts][:rows] + [""] * max(0, rows - len(texts))
    # Beside a merged cell: beside its last column on the right, its first on the left.
    col = cell.col + cell.colspan - 1 if where == "right" else cell.col
    added = col + 1 if where == "right" else col
    starts = {c.row: c for c in _new_cells(table, col, added)}
    for r, text in enumerate(texts):
        if text and r not in starts:
            raise EditRejected(f"Row {r + 1} has no cell of its own in the new column: a merged cell there gets wider instead. Leave it empty.")

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
        m = starts[r]  # the cell it's beside, whose look it takes (as tall as it)
        new = Cell(r, m.col, m.rowspan, m.colspan, (nx0, table.rows[r], nx1, table.rows[r + m.rowspan]))
        placing = _placing(table, r, new, table.rows[r])
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


# Resizing a column ------------------------------------------------------------------------------
#
# A column edge dragged left or right: the cells on its two sides get wider and narrower by
# as much (the table keeps its width), or, for the table's outer edge, its end column does
# (the table grows or shrinks). Cells don't wrap their text: a cell can get narrower only by
# its empty space, its text keeping the side it keeps to (or its middle) and the table's own
# margin, its pictures still fitting; and the table grows only into empty room on the page.
# Moving the edges is what adding a column does (_plan_reshape), without the new column.

MIN_COLUMN = 12.0  # points: an empty cell can't get narrower than this


def _cell_room(cell, margin: float, pictures) -> float:
    """How much narrower a cell can get, its text keeping its side and margin, its pictures
    (which keep their left side) still fitting."""
    x0, y0, x1, y1 = cell.bbox
    room = [x1 - x0 - MIN_COLUMN] + _room(cell, margin)
    for b in pictures:
        if x0 <= (b[0] + b[2]) / 2 <= x1 and y0 <= (b[1] + b[3]) / 2 <= y1:
            room.append(x1 - b[2] - margin)
    return max(0.0, min(room))


def _beside_table(page: pymupdf.Page, table: Table, lines: list[Line]) -> tuple[float, float]:
    """How far the table could grow on each side (as displayed): to the page's text area,
    or to the nearest thing beside it (text, a picture, a line or a shape)."""
    columns = [l.column for l in lines if l.column]
    x0, y0, x1, y1 = table.bbox
    lo, hi = (min(c[0] for c in columns), max(c[1] for c in columns)) if columns else (x0, x1)
    lo, hi = min(lo, x0), max(hi, x1)
    own = {l.id for l in table.lines}
    things = [l.bbox for l in lines if l.id not in own]
    things += [tuple(im["bbox"]) for im in page.get_image_info()]
    area = (x1 - x0) * (y1 - y0)
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"]) * page.rotation_matrix
        r.normalize()
        if r.width * r.height > 4 * area:
            continue  # a background behind the page
        things.append(tuple(r))
    for b in things:
        if b[3] <= y0 + 0.5 or b[1] >= y1 - 0.5:
            continue  # above or below the table
        if b[0] >= x0 - PAD and b[2] <= x1 + PAD:
            continue  # the table's own
        if b[0] >= x1 - 0.5:
            hi = min(hi, b[0])
        elif b[2] <= x0 + 0.5:
            lo = max(lo, b[2])
    return lo, hi


def border_limits(page: pymupdf.Page, table: Table, lines: list[Line]) -> list[tuple[float, float]]:
    """How far each column edge (as displayed, left to right) can go: (lowest, highest),
    whole points from where it is."""
    n = len(table.cols) - 1
    margin = _margin(table)
    t0, t1 = table.bbox[1], table.bbox[3]
    pictures = [tuple(im["bbox"]) for im in page.get_image_info() if t0 <= (im["bbox"][1] + im["bbox"][3]) / 2 <= t1]
    lo_page, hi_page = _beside_table(page, table, lines)
    out = []
    for j in range(n + 1):
        x = table.cols[j]
        ending = [c for c in table.cells if c.col + c.colspan == j]  # left of the edge
        starting = [c for c in table.cells if c.col == j]  # right of it
        left = min((_cell_room(c, margin, pictures) for c in ending), default=None)
        right = min((_cell_room(c, margin, pictures) for c in starting), default=None)
        if left is None:
            left = x - lo_page  # the table's left edge: as far as the page has room
        if right is None:
            right = hi_page - x  # its right edge
        out.append((x - math.floor(max(0.0, left)), x + math.floor(max(0.0, right))))
    return out


def resize_column(src: bytes, page_no: int, line_id: str, old_text: str, edge: int, x: float) -> EditResult:
    """A column edge of the table a line is in (0: the table's left edge) moved to `x` (as
    displayed), by whole points."""
    doc, page, pdf, rp = _open(src, page_no)
    lines = page_lines(page)
    table = locate(page, lines, line_id, old_text).table
    n = len(table.cols) - 1
    if not 0 <= edge <= n:
        raise EditRejected("That column edge isn't in this table.")
    d = round(x - table.cols[edge])
    if not d:
        raise EditRejected("It's still in the same place.")
    lo, hi = border_limits(page, table, lines)[edge]
    new_x = table.cols[edge] + d
    if new_x < lo - 0.01 or new_x > hi + 0.01:
        if (edge == 0 and d < 0) or (edge == n and d > 0):
            raise EditRejected("There's no room on the page for the table to get wider there.")
        raise EditRejected("A column can't get narrower than its text: cells don't wrap their text onto another line.")
    new = list(table.cols)
    new[edge] = new_x
    shape = Reshape(list(table.cols), new, list(range(n)), list(range(n + 1)))
    plan = _plan_reshape(page, rp, table, shape)
    _apply_reshape(pdf, page_no, plan)
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()
    area = _verify_reshape(src, result, page_no, plan, table, shape, lines)
    return EditResult(result, [], "", [(page_no, *area)], [(page_no, *plan.new_box)])
