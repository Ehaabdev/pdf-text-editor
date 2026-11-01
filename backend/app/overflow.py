"""When a page is full, what doesn't fit flows onto the next page.

Making room below a line (moving.shift_below) is refused when what's below would run past
the page's end: its bottom margin, or its footer. Here, instead, the lines that no longer
fit go to the top of the next page, under its header, and that page's own text moves down
to make room for them (and so on, page after page; past the last page, a new page with the
same size, header and footer). In steps, each checked, every other page unchanged:

1. Room on the next page: this again (it may flow on), or a new last page.
2. The lines drawn again there (pagecopy.py), with their underlines and links: each glyph
   where it was, as far up as the move; drawn alone, pixel for pixel what they were (MuPDF
   and PDFium, at a size where the move is whole pixels); nothing else there changed.
3. The lines taken off this page: the page pixel for pixel as it would be drawn without
   them; their links gone with them.
4. The room made on this page, as when nothing flows on.

What flows on is whole lines (with what's drawn with them), all below what stays. Refused,
with the reason: text side by side in columns, a table cell or a border the page's end
cuts through (rows flow later), a next page of another size or turned, more text than one
page holds, and more pages to flow over than MAX_PAGES.
"""

import io

import numpy as np
import pikepdf
import pymupdf

from . import moving, pagecopy
from .changes import EditRejected, contents, fail, outside, page_links, render, to_display, to_pdf
from .moving import MoveRefused, Overflow, Piece
from .pdfcontent import Replay, Unsupported, matches_trace, replay
from .textlayout import Line, page_lines

MAX_PAGES = 20  # pages text may flow over, one after another


# Headers and the body's top ------------------------------------------------------------------


def header_lines(doc: pymupdf.Document, page_no: int, lines: list[Line]) -> set[str]:
    """The ids of a page's header lines (as moving.bottom_limit finds footers): lines near
    its top repeated at the same place on the pages around, and lines wholly inside its top
    margin (as wide as the narrower side margin, as in most documents)."""
    page = doc[page_no]
    height = page.rect.height
    width = page.rect.width
    body = [l for l in lines if 0.1 * height < l.bbox[1] and l.bbox[3] < 0.9 * height]
    margin = min(min(l.bbox[0] for l in body), width - max(l.bbox[2] for l in body)) if body else 72.0
    margin = min(max(margin, 18.0), 108.0)
    pools = []
    for n in (page_no + 1, page_no - 1, page_no + 2, page_no - 2):
        if 0 <= n < doc.page_count and len(pools) < 3:
            other = doc[n]
            if abs(other.rect.width - page.rect.width) <= 1 and abs(other.rect.height - height) <= 1:
                pools.append(page_lines(other))
    out = {l.id for l in lines if l.bbox[3] < margin - 1}
    for l in lines:
        if l.bbox[3] > 0.25 * height:
            continue
        s = moving._sig(l.text)
        if any(moving._sig(o.text) == s and abs(o.bbox[1] - l.bbox[1]) <= 2 and abs(o.bbox[3] - l.bbox[3]) <= 2 for pool in pools for o in pool):
            out.add(l.id)
    return out


def _bands(lines: list[Line], ids: set[str], page) -> list[tuple[float, float]]:
    """The PDF y ranges of some lines: what's drawn at their height belongs with them."""
    out = []
    for l in lines:
        if l.id in ids:
            b = to_pdf(page, l.bbox)
            out.append((b[1], b[3]))
    return out


def _ink(rp: Replay, p: Piece) -> tuple:
    """Where a piece leaves ink (PDF): text by its letters."""
    if p.kind == "text":
        boxes = [moving._glyph_box(rp.glyphs[t]) for t in p.glyphs if not moving._blank(rp, rp.glyphs[t])]
        if not boxes:
            return None
        return moving._union(boxes)
    return p.box


def _body(doc: pymupdf.Document, page_no: int, rp: Replay) -> tuple[list, list, float | None]:
    """A page's body: its pieces that aren't its header or footer, a background or a clip;
    the header's and footer's pieces; and the top of the body (PDF y), None when empty."""
    page = doc[page_no]
    lines = page_lines(page)
    crop = (0.0, 0.0, page.rect.width, page.rect.height)
    _, footer = moving.bottom_limit(doc, page_no, lines, crop)
    header = header_lines(doc, page_no, lines)
    bands = _bands(lines, footer | header, page)
    body, edges = [], []
    w, h = page.rect.width, page.rect.height
    for p in moving._pieces(rp):
        if p.kind == "clip":
            continue
        ink = _ink(rp, p)
        if ink is None:
            continue
        if p.kind in ("path", "image") and (ink[2] - ink[0]) * (ink[3] - ink[1]) > 0.5 * w * h:
            continue  # a background behind the page
        if any(min(ink[3], b1) > max(ink[1], b0) for b0, b1 in bands):
            edges.append(p)
        else:
            body.append(p)
    top = max((_ink(rp, p)[3] for p in body), default=None)
    return body, edges, top


# Steps, each checked -------------------------------------------------------------------------


def _open(src: bytes, page_no: int):
    doc = pymupdf.open("pdf", src)
    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported:
        raise MoveRefused("the next page is built in a way that can't take more text yet")
    if not matches_trace(rp, doc[page_no]) or rp.stray_restore or rp.open_text:
        raise MoveRefused("the next page is built in a way that can't take more text yet")
    return doc, pdf, rp


def _other_pages_same(old: bytes, new: bytes, changed: set[int]) -> None:
    """Every page but these as it was (a page added at the end is new)."""
    a, b = pikepdf.open(io.BytesIO(old)), pikepdf.open(io.BytesIO(new))
    if len(b.pages) < len(a.pages):
        fail("a page would have gone")
    for i, (x, y) in enumerate(zip(a.pages, b.pages)):
        if i not in changed and contents(x) != contents(y):
            fail("another page changed")


def _save(pdf: pikepdf.Pdf) -> bytes:
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _links_along(pdf: pikepdf.Pdf, src_no: int, dst_no: int, rp: Replay, glyphs: set[int], dy: float) -> set[int]:
    """Links on the moving glyphs, drawn again on the other page (dy lower): the indexes
    of the old ones in their page's /Annots. A link that also covers other text: refused."""
    gone = set()
    annots = pdf.pages[src_no].obj.get("/Annots") or []
    for link in page_links(pdf, src_no, rp):
        under = link.glyphs & glyphs
        if not under:
            continue
        if under != link.glyphs:
            raise MoveRefused("a link covers text on both sides of the page's end")
        a = annots[link.index]
        copy = pikepdf.Dictionary({k: v for k, v in a.items()})
        x0, y0, x1, y1 = link.rect
        copy.Rect = pikepdf.Array([x0, y0 - dy, x1, y1 - dy])
        if "/QuadPoints" in copy:
            q = [float(v) for v in copy.QuadPoints]
            copy.QuadPoints = pikepdf.Array([v if k % 2 == 0 else v - dy for k, v in enumerate(q)])
        copy.P = pdf.pages[dst_no].obj
        dst = pdf.pages[dst_no].obj
        if "/Annots" not in dst:
            dst.Annots = pdf.make_indirect(pikepdf.Array())
        dst.Annots.append(pdf.make_indirect(copy))
        gone.add(link.index)
    for a in pdf.pages[src_no].obj.get("/Annots") or []:
        if a.get("/Subtype") == pikepdf.Name.Widget:
            r = [float(v) for v in a.Rect]
            box = moving._union([moving._glyph_box(rp.glyphs[t]) for t in glyphs]) if glyphs else None
            if box and min(max(r[0], r[2]), box[2]) > max(min(r[0], r[2]), box[0]) and min(max(r[1], r[3]), box[3]) > max(min(r[1], r[3]), box[1]):
                raise MoveRefused("a form field is in the text that would move to the next page")
    return gone


def copy_onto(src: bytes, src_no: int, dst_no: int, starts: set[int], dy: float) -> tuple[bytes, list]:
    """Pieces of page `src_no` (by their first instruction) drawn again on page `dst_no`,
    `dy` points lower (PDF; higher when negative), with their links. Checked. Returns the
    file and the area (page, as displayed) that changed."""
    doc, pdf, rp = _open(src, src_no)
    _, _, dst_rp = _open(src, dst_no)
    pieces = [p for p in moving._pieces(rp) if p.start in starts]
    glyphs = {t for p in pieces for t in p.glyphs}
    pagecopy.copy_pieces(pdf, rp, src_no, dst_no, pieces, dy, dst_rp)
    _links_along(pdf, src_no, dst_no, rp, glyphs, dy)
    new = _save(pdf)

    # Every glyph of the other page as it was, then the new ones: each where it was, dy lower.
    new_doc = pymupdf.open("pdf", new)
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), dst_no)
    except Unsupported:
        fail("the page can't be read back")
    if not matches_trace(new_rp, new_doc[dst_no]):
        fail("the page can't be read back")
    moved = [rp.glyphs[t] for t in sorted(glyphs)]
    if len(new_rp.glyphs) != len(dst_rp.glyphs) + len(moved):
        fail("the text on the next page would have changed")
    for a, b in zip(dst_rp.glyphs, new_rp.glyphs):
        if a.code != b.code or not moving._close(a.origin, b.origin, 0.01):
            fail("the text on the next page would have changed")
    # (Fonts compared in the new file: saving numbers objects again.)
    new_pdf = pikepdf.open(io.BytesIO(new))
    res_a = new_pdf.pages[src_no].obj.get("/Resources") or new_pdf.pages[src_no].Resources
    res_b = new_pdf.pages[dst_no].obj.Resources
    for a, b in zip(moved, new_rp.glyphs[len(dst_rp.glyphs):]):
        same_font = res_a.Font[a.paint.font].objgen == res_b.Font[b.paint.font].objgen
        if a.code != b.code or not same_font or abs(a.paint.size - b.paint.size) > 1e-6 or not moving._close(b.origin, (a.origin[0], a.origin[1] - dy), 0.01):
            fail("the text moved to the next page would have changed")

    # Drawn alone, what moved looks as it did, at a size where the move is whole pixels.
    page = doc[src_no]
    shown = lambda b: to_display(page, b)
    block = moving._union([p.box for p in pieces])
    area = shown(block) + (-2, -2, 2, 2)
    down = shown((0, block[1] - dy, 1, block[1] - dy)).y0 - shown((0, block[1], 1, block[1])).y0
    zoom = max(1, round(2 * abs(down))) / abs(down) if abs(down) > 1e-6 else 2.0
    keep_old = {k for k, p in enumerate(moving._pieces(rp)) if p.start in starts}
    # The new pieces: drawn after the page's own (wrapped in q ... Q, its open states closed).
    boundary = len(dst_rp.instructions) + dst_rp.open_states + 2
    keep_new = {k for k, p in enumerate(moving._pieces(new_rp)) if p.start >= boundary}
    alone_old, alone_new = moving._only(src, src_no, keep_old), moving._only(new, dst_no, keep_new)

    def differs(z: float, allow: int = 0) -> list[bool]:
        old_mu, old_pf = render(alone_old, src_no, z)
        new_mu, new_pf = render(alone_new, dst_no, z)
        sy = round(down * z)
        # PDFium rounds glyph positions to whole pixels: a pixel of slack, as in moving.py.
        return [moving._differs_after_shift(old_mu, new_mu, area, z, 0, sy, 0), moving._differs_after_shift(old_pf, new_pf, area, z, 0, sy, 1, allow)]

    first = differs(zoom)
    # A glyph exactly between two pixels can round either way; at twice the size it's on
    # a pixel. Each engine must match at one size: MuPDF exactly, PDFium within a few
    # pixels once MuPDF has matched exactly (as columns.py).
    if any(first) and any(a and b for a, b in zip(first, differs(2 * zoom, allow=4 if not first[0] else 0))):
        fail("the text moved to the next page wouldn't look exactly the same")

    # Nothing else on that page changed.
    there = to_display(doc[dst_no], (block[0], block[1] - dy, block[2], block[3] - dy)) + (-2, -2, 2, 2)
    a_mu, a_pf = render(src, dst_no)
    b_mu, b_pf = render(new, dst_no)
    if outside(a_mu, b_mu, [tuple(there)], 2.0, 0) or outside(a_pf, b_pf, [tuple(there)], 2.0, 1):
        fail("other parts of the next page would have changed")
    _other_pages_same(src, new, {dst_no})
    return new, [(dst_no, *tuple(there))]


def take_off(src: bytes, page_no: int, starts: set[int]) -> bytes:
    """Pieces of a page (by their first instruction) taken off it, with their links. Checked:
    the page is pixel for pixel what it would be drawn without them."""
    doc, pdf, rp = _open(src, page_no)
    pieces = [p for p in moving._pieces(rp) if p.start in starts]
    glyphs = {t for p in pieces for t in p.glyphs}
    links = [l.index for l in page_links(pdf, page_no, rp) if l.glyphs and l.glyphs <= glyphs]
    pagecopy.remove_pieces(pdf, rp, page_no, pieces)
    annots = pdf.pages[page_no].obj.get("/Annots")
    for i in sorted(links, reverse=True):
        del annots[i]
    new = _save(pdf)
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    left = [g for t, g in enumerate(rp.glyphs) if t not in glyphs]
    if len(new_rp.glyphs) != len(left) or any(a.code != b.code or not moving._close(a.origin, b.origin, 0.01) for a, b in zip(left, new_rp.glyphs)):
        fail("the text left on the page would have changed")
    keep = {k for k, p in enumerate(moving._pieces(rp)) if p.start not in starts}
    want_mu, want_pf = render(moving._only(src, page_no, keep), page_no)
    got_mu, got_pf = render(new, page_no)
    if want_mu.shape != got_mu.shape or np.any(want_mu != got_mu) or np.any(np.abs(want_pf.astype(int) - got_pf.astype(int)) > 0):
        fail("the page wouldn't look as it should without the text that moved")
    _other_pages_same(src, new, {page_no})
    return new


def new_page_after(src: bytes, page_no: int, leaving: set[int] = frozenset()) -> tuple[bytes, list[str]]:
    """A new last page like page `page_no` (the last): its size, its header and footer
    drawn again; nothing else (not the pieces `leaving` for it, by their first instruction,
    even in the footer's band). Notes for the user (printed page numbers)."""
    doc, pdf, rp = _open(src, page_no)
    page = pdf.pages[page_no]
    new = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=page.mediabox, Resources=pikepdf.Dictionary(), Contents=pdf.make_stream(b""))
    for key in ("/CropBox", "/Rotate", "/Group"):
        if key in page.obj:
            new[key] = page.obj[key]
    pdf.pages.append(pikepdf.Page(new))
    with_page = _save(pdf)
    _, edges, _ = _body(doc, page_no, rp)
    edges = [p for p in edges if p.start not in leaving]
    notes = []
    lines = page_lines(doc[page_no])
    _, footer = moving.bottom_limit(doc, page_no, lines, (0.0, 0.0, doc[page_no].rect.width, doc[page_no].rect.height))
    header = header_lines(doc, page_no, lines)
    if any(any(ch.isdigit() for ch in l.text) for l in lines if l.id in footer | header):
        notes.append("A new page was added at the end, with the same header and footer: check its page number, and the page count on the other pages.")
    else:
        notes.append("A new page was added at the end, with the same header and footer.")
    if not edges:
        return with_page, notes
    out, _ = copy_onto(with_page, page_no, len(pdf.pages) - 1, {p.start for p in edges}, 0.0)
    return out, notes


# Making room, flowing on ---------------------------------------------------------------------


def make_room(src: bytes, page_no: int, anchor, size: float, cut: float, dy: float, column=None, depth: int = 0):
    """As moving.shift_below (the new file, the areas that changed, how many lines moved,
    the shift; None when nothing is below), but what runs past the page's end flows onto
    the next page. The shift's `notes` say what the user should know."""
    try:
        made = moving.shift_below(src, page_no, anchor, size, cut, dy, column, split=True)
    except Overflow as o:
        return flow(src, page_no, o, anchor, size, cut, dy, column, depth)
    if made is not None:
        made[3].notes = getattr(made[3], "notes", [])
    return made


def _text_area(doc: pymupdf.Document, page_no: int) -> tuple[float, float]:
    cols = [l.column for l in page_lines(doc[page_no]) if l.column]
    page = doc[page_no]
    return (min(c[0] for c in cols), max(c[1] for c in cols)) if cols else (0.0, page.rect.width)


def may_flow(src: bytes, page_no: int, anchor) -> None:
    """Refused unless text at `anchor` (PDF) runs across the page's text area: text side by
    side in columns flows down its column, not onto the next page; a table cell holds its own."""
    doc, pdf, rp = _open(src, page_no)
    page = doc[page_no]
    cx0, cx1, cell = moving.column_of(anchor, rp, moving._crop(pdf, page_no))
    if cell:
        raise MoveRefused("the table cell is full (cells don't grow yet)")
    a0, a1 = _text_area(doc, page_no)
    lo, hi = to_display(page, (cx0, 0, cx1, 0)).x0, to_display(page, (cx0, 0, cx1, 0)).x1
    if hi - lo < 0.8 * (a1 - a0):
        raise MoveRefused("this text is in a column beside other text, so it can't flow onto the next page")


def send_on(src: bytes, page_no: int, starts: set[int], above: float, size: float, depth: int = 0, also: set[int] = frozenset(), take: bool = True) -> tuple[bytes, list, list[str], int, float]:
    """Pieces of a page (by their first instruction), whole lines at its end, moved to the
    top of the next page, under its header, as far below it as they were below `above` (PDF
    y: the text that stays): that page's own text moves down to make room (and flows on), or
    a new last page. `also`: pieces drawn there too that stay here as well (the border
    between a table's rows that stay and those that go). With `take` false, they're only
    drawn there, and stay here too. Returns the file, the areas that
    changed, notes, the page, and how far down they were drawn there (PDF; up when
    negative)."""
    if depth >= MAX_PAGES:
        raise MoveRefused(f"the text would flow over more than {MAX_PAGES} pages")
    doc, pdf, rp = _open(src, page_no)
    over = [p for p in moving._pieces(rp) if p.start in starts | also]
    inks = [_ink(rp, p) for p in over if _ink(rp, p)]
    block_top = max(b[3] for b in inks)
    block_bottom = min(b[1] for b in inks)
    gap = max(0.0, above - block_top)
    # What comes after keeps the gap that was over the block; under a table's last row (no
    # gap: its rows touch), a little room, about as under a line of text.
    lowest = min((p for p in over if _ink(rp, p)), key=lambda p: _ink(rp, p)[1])
    height = block_top - block_bottom + (gap if lowest.kind == "text" or gap > 0.5 else 0.4 * size)
    notes = []
    areas = []
    current = src
    target = page_no + 1
    if target < doc.page_count:
        a, b = doc[page_no], doc[target]
        if abs(a.rect.width - b.rect.width) > 1 or abs(a.rect.height - b.rect.height) > 1 or a.rotation != b.rotation:
            raise MoveRefused("the next page has another size, so the text can't flow onto it")
        _, _, trp = _open(current, target)
        body, _, top = _body(doc, target, trp)
        if top is None:
            _, _, top = _body(doc, page_no, rp)
        else:
            # Room at the top of the next page: everything from its body's top on moves down.
            crop = moving._crop(pdf, target)
            made = make_room(current, target, (crop[0], top - 1, crop[2], top), size, top + 0.5, height, (crop[0], crop[2]), depth + 1)
            if made is not None:
                current, moved_areas, _, shift = made
                areas += moved_areas
                notes += getattr(shift, "notes", [])
    else:
        current, added = new_page_after(current, page_no, starts)
        notes += added
        _, _, top = _body(doc, page_no, rp)
    if top is None:
        raise MoveRefused("the next page has no room for the text")
    floor, _ = moving.bottom_limit(pymupdf.open("pdf", current), target, page_lines(pymupdf.open("pdf", current)[target]), moving._crop(pdf, page_no))
    if top - (block_top - block_bottom) < floor - 0.01:
        raise MoveRefused("more text would flow on than one page holds")
    # The lines drawn there, at its body's top; then taken off this page.
    current, copied = copy_onto(current, page_no, target, starts | also, block_top - top)
    areas += copied
    if take:
        current = take_off(current, page_no, starts)
    return current, areas, notes, target, block_top - top


def flow(src: bytes, page_no: int, o: Overflow, anchor, size: float, cut: float, dy: float, column=None, depth: int = 0):
    """make_room, once moving.shift_below found what would run past the page's end (`o`)."""
    if column is None:
        may_flow(src, page_no, anchor)
    rp = _open(src, page_no)[2]
    both = {p.start for p in o.both}
    above = min((b[1] for b in (_ink(rp, p) for p in o.stay if p.start not in both) if b), default=cut)
    current, areas, notes, target, _ = send_on(src, page_no, {p.start for p in o.over}, above, size, depth, both)
    # Now the room here fits (nothing left to move when all that was below went on).
    made = moving.shift_below(current, page_no, anchor, size, cut, dy, column)
    if made is None:
        shift = moving.Shift(page_no, dy, [], [], tuple(anchor))
        made = (current, [], 0, shift)
    current, here, count, shift = made
    shift.notes = notes + [f"Text that no longer fit moved to page {target + 1}."]
    return current, here + areas, count, shift
