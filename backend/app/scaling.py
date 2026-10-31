"""A table made bigger or smaller as a whole, like a picture: everything drawn inside its
borders (text, borders, shading, pictures) scaled about its top corner, its left one (an
Arabic table's right one), so it looks the same, only smaller or bigger, text included.

Each piece is wrapped in q ... Q with a cm that scales it, as dragging.py moves a table
(what it set that lasts is set again after the Q). The new width goes by whole points.

Checked before it's accepted: every glyph exactly where the scale puts it, its size scaled;
every shape and picture of the table scaled, the others where they were (and MuPDF lists
the same shapes); nothing outside the table's old and new places changes by a pixel (MuPDF
and PDFium); every other line reads as before, the table's lines the same text at their
scaled places; and the page reads as the same table, its edges scaled, the same text in
each cell.

Refused, with the reason: text smaller than 5 points, a table growing past the page's
text area or onto something beside or below it, a table without borders (its lines move
instead), and whatever moving a table refuses (something drawn across its edge).
"""

import io
import math

import pikepdf
import pymupdf

from . import moving
from .changes import EditRejected, LinkChange, _drawings, check_links, check_other_pages, fail, outside, render, to_display, to_pdf, write_page
from .columns import _beside_table, locate
from .dragging import Plan, _plan
from .editing import EditResult, comparable
from .pdfcontent import Replay, Unsupported, inverse, matches_trace, mul, replay
from .tables import Table, find_tables
from .textlayout import Line, page_lines

MIN_SIZE = 5.0  # points: smaller text can't be read
MIN_SCALE = 0.3


def _things(page: pymupdf.Page, table: Table, lines: list[Line]) -> list[tuple]:
    """What's on the page besides the table (as displayed): text, pictures, shapes (not a
    background behind the page)."""
    own = {l.id for l in table.lines}
    x0, y0, x1, y1 = table.bbox
    out = [l.bbox for l in lines if l.id not in own]
    out += [tuple(im["bbox"]) for im in page.get_image_info()]
    area = (x1 - x0) * (y1 - y0)
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"]) * page.rotation_matrix
        r.normalize()
        if r.width * r.height > 4 * area:
            continue
        out.append(tuple(r))
    pad = 2.0
    return [b for b in out if not (b[0] >= x0 - pad and b[1] >= y0 - pad and b[2] <= x1 + pad and b[3] <= y1 + pad)]


def scale_limits(page: pymupdf.Page, table: Table, lines: list[Line]) -> tuple[float, float]:
    """How much smaller and bigger the table can get: (lowest, highest) scale."""
    x0, y0, x1, y1 = table.bbox
    w, h = x1 - x0, y1 - y0
    sizes = [l.style.size for l in table.lines]
    low = max(MIN_SCALE, MIN_SIZE / min(sizes)) if sizes else MIN_SCALE
    lo_page, hi_page = _beside_table(page, table, lines)
    high = (x1 - lo_page) / w if table.rtl else (hi_page - x0) / w
    # Below it: the nearest thing under where it would be at its widest, or the page's
    # bottom margin.
    wide = (x1 - w * high, x1) if table.rtl else (x0, x0 + w * high)
    bottom = page.rect.height - min(72.0, page.rect.width * 0.08)
    for b in _things(page, table, lines):
        if b[1] >= y1 - 0.5 and b[0] < wide[1] and b[2] > wide[0]:
            bottom = min(bottom, b[1])
    high = min(high, max(1.0, (bottom - y0) / h))
    # As whole points of width.
    low, high = math.ceil(w * low - 1e-6) / w, math.floor(w * high + 1e-6) / w
    return low, max(low, high)


def _scale_cm(p, s: float, ax: float, ay: float) -> tuple[list, list]:
    """A cm scaling what follows by s about (ax, ay) on the page (PDF), and its inverse."""
    page = (s, 0, 0, s, ax * (1 - s), ay * (1 - s))
    m = mul(mul(p.ctm, page), inverse(p.ctm))
    back = inverse(m)
    return [round(v, 9) for v in m], [round(v, 9) for v in back]


def _apply(pdf: pikepdf.Pdf, page_no: int, rp: Replay, plan: Plan, s: float, ax: float, ay: float) -> None:
    before: dict = {}
    after: dict = {}
    op = lambda operands, name: pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(name))
    for p in plan.pieces:
        go, back = _scale_cm(p, s, ax, ay)
        lasting = moving._lasting(rp, p)
        if lasting is not None:
            before.setdefault(p.start, []).extend([op([], "q"), op(go, "cm")])
            after.setdefault(p.end, [])[0:0] = [op([], "Q"), *lasting]
        else:
            before.setdefault(p.start, []).append(op(go, "cm"))
            after.setdefault(p.end, []).insert(0, op(back, "cm"))
    out = []
    for k, ins in enumerate(rp.instructions):
        out += before.get(k, [])
        out.append(ins)
        out += after.get(k, [])
    write_page(pdf, page_no, out)


def scale_table(src: bytes, page_no: int, line_id: str, old_text: str, width: float) -> EditResult:
    """The table a line is in made `width` points wide (as displayed), all of it scaled."""
    mu, page, pdf, lines, plan = _plan(src, page_no, [(line_id, old_text)], True)
    table = locate(page, lines, line_id, old_text).table
    x0, y0, x1, y1 = table.bbox
    w = x1 - x0
    new_w = round(width)
    if abs(new_w - w) < 1:
        raise EditRejected("It's still the same size.")
    s = new_w / w
    low, high = scale_limits(page, table, lines)
    if s < low - 1e-6:
        raise EditRejected("The table can't get that small: its text would be too small to read.")
    if s > high + 1e-6:
        raise EditRejected("There's no room on the page for the table to get that big: something is beside or below it, or the page's margin.")
    # About its top corner: the left one (an Arabic table's right one).
    ax, ay = to_pdf(page, (x1 if table.rtl else x0, y0, x1 if table.rtl else x0, y0))[:2]
    for b in plan.bounds:
        new = _scaled(plan.box, s, ax, ay)
        if not (new[0] >= b[0] - 0.5 and new[1] >= b[1] - 0.5 and new[2] <= b[2] + 0.5 and new[3] <= b[3] + 0.5):
            raise EditRejected("Part of it would be cut off: this PDF shows it only inside an area of the page (its margins, or a box).")
    rp = plan.rp
    _apply(pdf, page_no, rp, plan, s, ax, ay)
    changes = _links(pdf, page_no, plan, s, ax, ay)
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()
    areas = _verify(src, result, page_no, rp, plan, table, lines, changes, s, ax, ay)
    new_box = to_display(page, _scaled(to_pdf(page, table.bbox), s, ax, ay))
    return EditResult(result, [], "", [(page_no, *a) for a in areas], [(page_no, *tuple(new_box))])


def _scaled(box: tuple, s: float, ax: float, ay: float) -> tuple:
    a = (ax + (box[0] - ax) * s, ay + (box[1] - ay) * s)
    b = (ax + (box[2] - ax) * s, ay + (box[3] - ay) * s)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _links(pdf: pikepdf.Pdf, page_no: int, plan: Plan, s: float, ax: float, ay: float) -> list[LinkChange]:
    """Links on the table's text, scaled with it."""
    annots = pdf.pages[page_no].obj.Annots if plan.links else []
    changes = []
    for link in plan.links:
        a = annots[link.index]
        r = _scaled(link.rect, s, ax, ay)
        a.Rect = pikepdf.Array(list(r))
        if "/QuadPoints" in a:
            q = [float(v) for v in a.QuadPoints]
            a.QuadPoints = pikepdf.Array([ax + (v - ax) * s if k % 2 == 0 else ay + (v - ay) * s for k, v in enumerate(q)])
        changes.append(LinkChange(link, r))
    return changes


def _verify(old: bytes, new: bytes, page_no: int, rp: Replay, plan: Plan, table: Table, lines: list[Line], links: list, s: float, ax: float, ay: float) -> list[tuple]:
    old_doc, new_doc = pymupdf.open("pdf", old), pymupdf.open("pdf", new)
    old_page, new_page = old_doc[page_no], new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    if not matches_trace(new_rp, new_page):
        fail("the page can't be read back")
    point = lambda x, y: (ax + (x - ax) * s, ay + (y - ay) * s)

    # Every glyph of the table where the scale puts it, its size scaled; the others as they were.
    scaled = {t for p in plan.pieces for t in p.glyphs}
    if len(new_rp.glyphs) != len(rp.glyphs):
        fail("text on the page would have changed")
    for t, (a, b) in enumerate(zip(rp.glyphs, new_rp.glyphs)):
        want = point(*a.origin) if t in scaled else a.origin
        k = s if t in scaled else 1.0
        if (
            a.code != b.code
            or abs(want[0] - b.origin[0]) > 0.01
            or abs(want[1] - b.origin[1]) > 0.01
            or (a.paint.font, a.paint.size, a.paint.fill, a.paint.Tr) != (b.paint.font, b.paint.size, b.paint.fill, b.paint.Tr)
            or not moving._close([v * k for v in a.paint.trm2], b.paint.trm2, 1e-4)
        ):
            fail("text on the page would have changed")
    # Every shape and picture.
    paths = {p.index for p in plan.pieces if p.kind in ("path", "clip")}
    images = {p.index for p in plan.pieces if p.kind == "image"}
    if len(new_rp.paths) != len(rp.paths) or len(new_rp.images) != len(rp.images):
        fail("the page's shapes would have changed")
    for i, (a, b) in enumerate(zip(rp.paths, new_rp.paths)):
        want = _scaled(a.bbox, s, ax, ay) if i in paths else a.bbox
        if a.op != b.op or a.clip != b.clip or not moving._close(b.bbox, want, 0.02):
            fail("the page's shapes would have changed")
    for i, (a, b) in enumerate(zip(rp.images, new_rp.images)):
        want = _scaled(a.bbox, s, ax, ay) if i in images else a.bbox
        if not moving._close(b.bbox, want, 0.02):
            fail("the page's pictures would have changed")
    # MuPDF's own list of shapes agrees (what paints; a stroke's width scales too).
    old_boxes = [rp.paths[i].bbox for i in paths if rp.paths[i].fills or rp.paths[i].strokes]
    expected = []
    for look, r in _drawings(old_page):
        if any(moving._close(r, m, 0.15) for m in old_boxes):
            expected.append((look[:3], _scaled(r, s, ax, ay)))
        else:
            expected.append((look[:3], r))
    unused = [(look[:3], r) for look, r in _drawings(new_page)]
    if len(unused) != len(expected):
        fail("the page's lines and shapes changed")
    for look, rect in expected:
        k = next((n for n, (l, r) in enumerate(unused) if l == look and moving._close(r, rect, 0.2)), None)
        if k is None:
            fail("the page's lines and shapes changed")
        unused.pop(k)
    check_links(old_page, new_page, links)

    # Nothing outside the table's old and new places changes, in two engines.
    pad = (-2, -2, 2, 2)
    old_area = to_display(old_page, plan.box) + pad
    new_area = to_display(old_page, _scaled(plan.box, s, ax, ay)) + pad
    boxes = [tuple(old_area), tuple(new_area)]
    old_mu, old_pf = render(old, page_no)
    new_mu, new_pf = render(new, page_no)
    if outside(old_mu, new_mu, boxes, 2.0, 0) or outside(old_pf, new_pf, boxes, 2.0, 1):
        fail("other parts of the page would have changed")

    # Every line reads as before: the table's at their scaled places.
    ids = {l.id for l in plan.lines}
    shown = lambda b: to_display(old_page, _scaled(to_pdf(old_page, b), s, ax, ay))
    expect = sorted((l.text,) + tuple(round(v, 1) for v in (tuple(shown(l.bbox)) if l.id in ids else l.bbox)) for l in lines)
    got = sorted((l.text,) + tuple(round(v, 1) for v in l.bbox) for l in page_lines(new_page))
    if len(expect) != len(got) or any(e[0] != g[0] or not moving._close(e[1:], g[1:], 0.6) for e, g in zip(expect, got)):
        fail("the table's text would read differently")
    # And as the same table: its edges scaled (each shape was checked exactly above; the
    # reader may find a thick border a point or so off), the same text in each cell.
    new_bbox = shown(table.bbox)
    after = [t for t in find_tables(new_page, page_lines(new_page)) if t.ruled and abs(t.bbox[1] - new_bbox.y0) < 1 and abs(t.bbox[0] - new_bbox.x0) < 1]
    xs = [shown((x, table.bbox[1], x, table.bbox[1])).x0 for x in table.cols]
    ys = [shown((table.bbox[0], y, table.bbox[0], y)).y0 for y in table.rows]
    if len(after) != 1 or len(after[0].cols) != len(xs) or len(after[0].rows) != len(ys) or not moving._close(after[0].cols, xs, 2.0) or not moving._close(after[0].rows, ys, 2.0):
        fail("the page wouldn't read as the same table")
    cells = lambda t: sorted((c.row, c.col, c.rowspan, c.colspan, comparable(" ".join(l.text for l in c.lines))) for c in t.cells)
    if cells(after[0]) != cells(table):
        fail("the table's cells would read differently")
    check_other_pages(old, new, {page_no})
    return boxes
