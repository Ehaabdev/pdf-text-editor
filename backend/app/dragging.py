"""Drag lines, or a whole table, to another place on the same page. The old place is left
empty.

What moves: the chosen lines' text objects, each kept whole (a translation, cm, before
it and undone right after it, as moving.py moves the text below a new line), with their
underlines, strikethroughs, highlights and links; or a table with borders and everything
drawn inside them: its text, borders, shading and pictures. Drawing order, clipping and
the instructions themselves stay as they were.

Refused, with the reason, when it can't be done exactly: a line drawn as one with text
that wasn't chosen (the website then offers to move them together), something drawn
across a table's edge, a shape drawn together with others, a turned page, or a spot that
isn't empty or not on the page.

Checked before it's accepted, as a move to make room is (moving.verify): every glyph,
shape, picture and link is where it was or exactly as far away as the move; nothing
outside the old and new places changes by a pixel (MuPDF and PDFium); what moved looks
the same, pixel for pixel, at its new place; and every line reads the same. A move goes
by half points, so that it's a whole number of pixels both ways at twice the size.
"""

import io
from dataclasses import dataclass

import pikepdf
import pymupdf

from . import decorations, moving
from .changes import EditRejected, LinkChange, ink_box, page_links, to_display, to_pdf
from .editing import EditResult, comparable
from .moving import Box, MoveRefused, Piece, Shift
from .pdfcontent import Replay, Unsupported, matches_trace, replay
from .tables import find_tables
from .textlayout import Line, page_lines

GRID = 0.5  # points: a move snaps to it
EDGE = 2.0  # points: what's drawn this far outside a table's borders still belongs to it


class Together(EditRejected):
    """The chosen lines are drawn as one with other lines: they can only move with them."""

    def __init__(self, message: str, lines: list[Line]):
        super().__init__(message)
        self.lines = lines


@dataclass
class Plan:
    page_no: int
    rp: Replay
    pieces: list[Piece]  # what moves
    lines: list[Line]  # the lines that move, as read now
    box: Box  # PDF: where what moves can leave ink
    ink: Box  # PDF: what moves, drawn tight (to find a spot for it)
    links: list  # changes.Link: links that move with the text
    bounds: list  # PDF boxes of the clipping that stays: what moves must stay inside them


def _open(src: bytes, page_no: int):
    mu = pymupdf.open("pdf", src)
    if not 0 <= page_no < mu.page_count:
        raise EditRejected("That page doesn't exist.")
    page = mu[page_no]
    if page.rotation:
        raise EditRejected("Text on a turned page can't be moved yet.")
    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported:
        raise EditRejected("This page is built in a way that can't be moved safely yet.")
    if not matches_trace(rp, page) or rp.stray_restore or rp.open_text:
        raise EditRejected("This page is built in a way that can't be moved safely yet.")
    return mu, page, pdf, rp, page_lines(page)


def _chosen(lines: list[Line], targets: list[tuple[str, str]]) -> list[Line]:
    out = []
    for line_id, old_text in targets:
        line = next((l for l in lines if l.id == line_id), None)
        if line is None or comparable(line.text) != comparable(old_text):
            raise EditRejected("A line changed since you selected it. Select it again.")
        if line not in out:
            out.append(line)
    if not out:
        raise EditRejected("No lines were chosen.")
    return out


def _pieces(rp: Replay) -> list[Piece]:
    try:
        return moving._pieces(rp)
    except MoveRefused as e:
        raise EditRejected(f"This can't be moved: {e}.")


def _union(boxes) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _within(inner: Box, outer: Box, pad: float = 0.0) -> bool:
    return inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad


def _overlap(a: Box, b: Box, by: float = 0.0) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > by and min(a[3], b[3]) - max(a[1], b[1]) > by


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return f"“{text[:40]}…”" if len(text) > 40 else f"“{text}”"


def _clipping(rp: Replay, pieces: list[Piece], moving_pieces: list[Piece]) -> tuple[list[Piece], list[Box]]:
    """The clipping paths that apply to what moves: those that apply to nothing else move
    with it (a table cell's text clipped to its cell); the others stay, and what moves
    must stay inside them (Chrome shows a page's text only inside its margins)."""
    scopes = moving._clip_scopes(rp)
    painted = [p for p in pieces if p.kind != "clip"]
    ids = {(p.kind, p.start) for p in moving_pieces}
    along, bounds = [], []
    for p in pieces:
        if p.kind != "clip" or (p.kind, p.start) in ids:
            continue
        end = scopes.get(p.index, len(rp.instructions))
        under = [q for q in painted if p.end < q.start < end]
        if not any((q.kind, q.start) in ids for q in under):
            continue
        if all((q.kind, q.start) in ids for q in under):
            along.append(p)
        else:
            bounds.append(p.box)
    return along, bounds


def _finish(page_no: int, rp: Replay, pdf: pikepdf.Pdf, pieces: list[Piece], moving_pieces: list[Piece], lines: list[Line]) -> Plan:
    along, bounds = _clipping(rp, pieces, moving_pieces)
    moving_pieces = sorted(moving_pieces + along, key=lambda p: p.start)
    glyphs = {t for p in moving_pieces for t in p.glyphs}
    moved_lines = [l for l in lines if set(l.members) & glyphs]
    for l in moved_lines:
        if not set(l.members) <= glyphs:
            raise EditRejected(f"The line {_quote(l.text)} is drawn partly with what you chose: it can't be split.")
    links = []
    for link in page_links(pdf, page_no, rp):
        under = link.glyphs & glyphs
        if under and under != link.glyphs:
            raise EditRejected("A link covers both the chosen text and other text, so they can't be moved apart.")
        if under:
            links.append(link)
    shapes = [p.box for p in moving_pieces if p.kind in ("path", "image")]  # not clips: they paint nothing
    tight = [p.box for p in moving_pieces if p.kind == "text"]
    ink = _union(tight + shapes)
    box = _union(([ink_box(rp, sorted(glyphs))] if glyphs else []) + shapes + [ink])
    return Plan(page_no, rp, moving_pieces, moved_lines, box, ink, links, bounds)


def plan_lines(page_no: int, rp: Replay, pdf: pikepdf.Pdf, lines: list[Line], chosen: list[Line]) -> Plan:
    """What moves with these lines: their text, drawn whole, and their decorations."""
    pieces = _pieces(rp)
    glyphs = {t for l in chosen for t in l.members}
    owner = {t: l for l in lines for t in l.members}
    moving_pieces, along = [], []
    for p in pieces:
        if p.kind != "text" or not set(p.glyphs) & glyphs:
            continue
        for t in p.glyphs:
            if t in glyphs:
                continue
            other = owner.get(t)
            if other is not None:
                if other not in along:
                    along.append(other)
            elif not moving._blank(rp, rp.glyphs[t]):
                raise EditRejected("Something drawn together with these lines can't be moved with them.")
        moving_pieces.append(p)
    if along:
        names = ", ".join(_quote(l.text) for l in along[:3]) + (f" and {len(along) - 3} more" if len(along) > 3 else "")
        raise Together(f"The PDF stores these lines as one with {names}: they can only move together.", along)
    paths = set()
    for l in chosen:
        for d in decorations.for_line(l, rp):
            if d.shared:
                raise EditRejected("An underline or highlight here is drawn together with other shapes, so it can't move with its text yet.")
            if not d.traces <= glyphs:
                continue
            paths.add(d.path)
    moving_pieces += [p for p in pieces if p.kind == "path" and p.index in paths]
    moving_pieces.sort(key=lambda p: p.start)
    return _finish(page_no, rp, pdf, pieces, moving_pieces, lines)


def plan_table(page_no: int, page: pymupdf.Page, rp: Replay, pdf: pikepdf.Pdf, lines: list[Line], line: Line) -> Plan:
    """What moves with the table a line is in: everything drawn inside its borders."""
    table = next((t for t in find_tables(page, lines) if any(l.id == line.id for l in t.lines)), None)
    if table is None:
        raise EditRejected("This line isn't in a table.")
    if not table.ruled:
        # A table without borders is its lines.
        return plan_lines(page_no, rp, pdf, lines, table.lines)
    inside = to_pdf(page, table.bbox)
    x0, y0, x1, y1 = inside
    edge = (x0 - EDGE, y0 - EDGE, x1 + EDGE, y1 + EDGE)

    def centre_in(t: int) -> bool:
        b = moving._glyph_box(rp.glyphs[t])
        return x0 <= (b[0] + b[2]) / 2 <= x1 and y0 <= (b[1] + b[3]) / 2 <= y1

    pieces = _pieces(rp)
    moving_pieces = []
    for p in pieces:
        if not _overlap(p.box, edge):
            continue
        if p.kind == "text":
            # By where its letters are: text just above or below the table isn't in it.
            seen = [centre_in(t) for t in p.glyphs if not moving._blank(rp, rp.glyphs[t])]
            if all(seen) and (seen or _within(p.box, edge)):
                moving_pieces.append(p)
            elif any(seen):
                raise EditRejected("The table can't be moved: text is drawn across its edge.")
        elif _within(p.box, edge):
            moving_pieces.append(p)
        elif not _overlap(p.box, inside, 0.5) or p.kind == "clip" or (p.background and _within(edge, p.box)):
            continue  # beside it, clipping, or a background the whole table is on: stays
        else:
            what = "a picture" if p.kind == "image" else "a line or a shape"
            raise EditRejected(f"The table can't be moved: {what} is drawn across its edge.")
    if not moving_pieces:
        raise EditRejected("This table has nothing that can be moved.")
    return _finish(page_no, rp, pdf, pieces, moving_pieces, lines)


def _plan(src: bytes, page_no: int, targets: list[tuple[str, str]], table: bool):
    mu, page, pdf, rp, lines = _open(src, page_no)
    chosen = _chosen(lines, targets)
    plan = plan_table(page_no, page, rp, pdf, lines, chosen[0]) if table else plan_lines(page_no, rp, pdf, lines, chosen)
    return mu, page, pdf, lines, plan


def preview(src: bytes, page_no: int, targets: list[tuple[str, str]], table: bool = False, width: int = 1600) -> tuple[bytes, tuple, list[str]]:
    """A picture of what would move, alone on a see-through background, the box it covers
    (as displayed) and the ids of the lines that move."""
    mu, page, pdf, lines, plan = _plan(src, page_no, targets, table)
    order = {(p.kind, p.start) for p in plan.pieces}
    keep = {n for n, p in enumerate(_pieces(plan.rp)) if (p.kind, p.start) in order}
    alone = pymupdf.open("pdf", moving._only(src, page_no, keep))[page_no]
    box = to_display(page, plan.box) & page.rect
    zoom = max(0.5, min(3.0, width / max(box.width, 1.0)))
    pix = alone.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=box, alpha=True)
    return pix.tobytes("png"), tuple(round(v, 2) for v in box), [l.id for l in plan.lines]


def _snap(v: float) -> float:
    return round(v / GRID) * GRID


def _check_spot(page: pymupdf.Page, pdf: pikepdf.Pdf, plan: Plan, pieces: list[Piece], dx: float, dy: float) -> None:
    """The new place is on the page and empty."""
    x0, y0, x1, y1 = plan.ink
    new = (x0 + dx, y0 - dy, x1 + dx, y1 - dy)
    crop = moving._crop(pdf, plan.page_no)
    if not _within(new, crop):
        raise EditRejected("It would go off the page. Drop it where all of it fits.")
    for b in plan.bounds:
        if not _within(new, b, 0.5):
            raise EditRejected("Part of it would be cut off there: this PDF shows it only inside an area of the page (its margins, or a box). Drop it inside.")
    old = plan.ink
    moving_ids = {(p.kind, p.start) for p in plan.pieces}
    for p in pieces:
        if (p.kind, p.start) in moving_ids or p.kind == "clip":
            continue
        if p.kind == "text":
            ink = [moving._glyph_box(plan.rp.glyphs[t]) for t in p.glyphs if not moving._blank(plan.rp, plan.rp.glyphs[t])]
            if any(_overlap(b, new, 0.3) for b in ink):
                raise EditRejected("It would land on other text. Drop it in an empty spot.")
        elif _overlap(p.box, new, 0.3):
            if p.kind == "path" and p.background and _within(new, p.box) and _within(old, p.box):
                continue  # a background under both places
            what = "a picture" if p.kind == "image" else "a line or a shape"
            raise EditRejected(f"It would land on {what}. Drop it in an empty spot.")
    for a in pdf.pages[plan.page_no].obj.get("/Annots") or []:
        subtype = a.get("/Subtype")
        if subtype in (pikepdf.Name.Link, pikepdf.Name.Popup) or "/Rect" not in a:
            continue
        r = [float(v) for v in a.Rect]
        if _overlap((min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])), new):
            raise EditRejected("A form field is in the way." if subtype == pikepdf.Name.Widget else "A comment or stamp is in the way.")


def _move_links(pdf: pikepdf.Pdf, page_no: int, links: list, dx: float, dy: float) -> list[LinkChange]:
    """Each link moved by the same, all its parts (an address over two lines has two)."""
    annots = pdf.pages[page_no].obj.Annots if links else []
    changes = []
    for link in links:
        a = annots[link.index]
        x0, y0, x1, y1 = link.rect
        a.Rect = pikepdf.Array([x0 + dx, y0 - dy, x1 + dx, y1 - dy])
        if "/QuadPoints" in a:
            q = [float(v) for v in a.QuadPoints]
            a.QuadPoints = pikepdf.Array([v + (dx if k % 2 == 0 else -dy) for k, v in enumerate(q)])
        changes.append(LinkChange(link, (x0 + dx, y0 - dy, x1 + dx, y1 - dy)))
    return changes


def _plain(why: str, joined: bool = True) -> str:
    """The check that failed, in words that say what to do."""
    if "read differently" in why:
        if joined:
            return "It would read as one line with the text beside it. Move it a little higher, lower or further away."
        return "Moved there, it would change how other text on this page is read. Try another spot."
    if "look exactly the same" in why:
        return "It wouldn't look exactly the same there (part of it would be cut off). Try another spot."
    return f"Not moved: a safety check found that {why}. Your page is unchanged."


def move(src: bytes, page_no: int, targets: list[tuple[str, str]], dx: float, dy: float, table: bool = False) -> EditResult:
    """Move the chosen lines (with `table`, the table the first of them is in) by dx, dy
    points as displayed (right and down)."""
    dx, dy = _snap(dx), _snap(dy)
    if not dx and not dy:
        raise EditRejected("It's still in the same place.")
    mu, page, pdf, lines, plan = _plan(src, page_no, targets, table)
    # As displayed -> PDF (a page that isn't turned: the same, upside down).
    a = to_pdf(page, (0, 0, 0, 0))
    b = to_pdf(page, (dx, dy, dx, dy))
    pdx, pdy = b[0] - a[0], a[1] - b[1]  # right, and down
    pieces = _pieces(plan.rp)
    _check_spot(page, pdf, plan, pieces, pdx, pdy)

    shift = Shift(page_no, pdy, plan.pieces, [], plan.box, [l.id for l in plan.lines], pdx, saved=True)
    moving.apply(pdf, page_no, plan.rp, shift)
    shift.links = _move_links(pdf, page_no, plan.links, pdx, pdy)
    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()
    try:
        areas = moving.verify(src, result, page_no, plan.rp, shift, lines)
    except EditRejected as e:
        why = str(e).removeprefix("Not saved: a safety check found that ").removesuffix(". Your page is unchanged.")
        raise EditRejected(_plain(why, _joined(result, page_no, plan, dx, dy))) from e

    x0, y0, x1, y1 = plan.ink
    new = to_display(page, (x0 + pdx, y0 - pdy, x1 + pdx, y1 - pdy))
    warnings = []
    if _left_behind(result, page_no, plan, lines, new):
        warnings.append("Something drawn under the moved text (a line or a colored box) stayed where it was. Check the page.")
    marks = [(page_no, *new)]
    return EditResult(result, warnings, "", areas, marks, moved=len(plan.lines))


def _joined(new_bytes: bytes, page_no: int, plan: Plan, dx: float, dy: float) -> bool:
    """Does a moved line read differently at its new place (as part of another)?"""
    now = page_lines(pymupdf.open("pdf", new_bytes)[page_no])
    for l in plan.lines:
        x0, y0, x1, y1 = l.bbox
        if not any(comparable(n.text) == comparable(l.text) and abs(n.bbox[0] - x0 - dx) < 0.5 and abs(n.bbox[1] - y0 - dy) < 0.5 for n in now):
            return True
    return False


def _left_behind(new_bytes: bytes, page_no: int, plan: Plan, lines: list[Line], new: pymupdf.Rect) -> bool:
    """A shape right under or over a moved line, in its old place, that touches no other
    text: an underline or a box not recognized as the text's own."""
    page = pymupdf.open("pdf", new_bytes)[page_no]
    moved = {l.id for l in plan.lines}
    remaining = [pymupdf.Rect(l.bbox) for l in lines if l.id not in moved]
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"]) * page.rotation_matrix
        r.normalize()
        if r.width < 2.5 and r.height > 4:
            continue  # a table's vertical border
        if r.intersects(new):
            continue  # at the new place: moved with the text
        for l in plan.lines:
            box = pymupdf.Rect(l.bbox)
            band = pymupdf.Rect(box.x0 - 1, box.y0 - 0.15 * l.style.size, box.x1 + 1, box.y1 + 0.3 * l.style.size)
            if band.contains(r) and not any(r.intersects(o) for o in remaining):
                return True
    return False
