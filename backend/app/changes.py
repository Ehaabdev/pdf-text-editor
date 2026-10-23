"""What every change to a page shares, whether words are edited or lines deleted.

  - The page's instructions are rewritten: glyphs are taken out (each replaced by an
    equal spacing number, so nothing else moves), decorations (underlines,
    strikethroughs, highlights) are dropped or redrawn under the words' new place, and
    a block of new text can be added.
  - Links follow their text: the clickable area moves with the words, a link whose
    words are all gone is removed, and a link whose target is its own text ("mailto:"
    plus the address) gets the new text as its target.
  - Independent checks before a change is accepted: pixels outside the changed area
    (MuPDF and PDFium), MuPDF's own list of the page's shapes and links, and the other
    pages' contents.
"""

import io
from dataclasses import dataclass

import numpy as np
import pikepdf
import pymupdf
import pypdfium2 as pdfium

from .pdfcontent import Matrix, Replay, inverse, mul, page_matrix, remove_glyphs


class EditRejected(Exception):
    """The change can't be made exactly. The message is for the user."""


def fail(why: str):
    raise EditRejected(f"Not saved: a safety check found that {why}. Your page is unchanged.")


@dataclass
class PathChange:
    """A decoration that moves with its words: drawn again over each new span (PDF x),
    or dropped when there are none."""

    path: int  # index into Replay.paths
    old: tuple[float, float]  # its ends now (PDF x)
    spans: list[tuple[float, float]]


def _stretch(ctm: Matrix, old: tuple[float, float], new: tuple[float, float]) -> Matrix:
    """A cm that, under this CTM, maps the old span onto the new one on the page."""
    s = (new[1] - new[0]) / (old[1] - old[0])
    page_map = (s, 0.0, 0.0, 1.0, new[0] - s * old[0], 0.0)
    return mul(mul(ctm, page_map), inverse(ctm))


def _op(operands, op) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(op))


def rewrite(rp: Replay, remove: set[int], insert_after: int | None = None, block: list = (), paths: list[PathChange] = ()) -> list:
    """The page's instructions with glyphs removed, decorations changed and a block
    of new instructions added after instruction `insert_after`."""
    rewritten = remove_glyphs(rp, remove)
    # remove_glyphs keeps one instruction per original one, except ' and " which become
    # several; count them to keep instruction numbers lined up.
    touched_ops = {rp.glyphs[i].op for i in remove}
    expansions = []
    for k, ins in enumerate(rp.instructions):
        op = str(ins.operator) if not isinstance(ins, pikepdf.ContentStreamInlineImage) else None
        expansions.append((2 if op == "'" else 4 if op == '"' else 1) if k in touched_ops else 1)
    replaced: dict[int, list] = {}  # first instruction of a path -> what goes there instead
    skipped: set[int] = set()
    for change in paths:
        p = rp.paths[change.path]
        original = rp.instructions[p.start : p.end + 1]
        new = []
        for span in change.spans:
            new += [_op([], "q"), _op(_stretch(p.ctm, change.old, span), "cm"), *original, _op([], "Q")]
        replaced[p.start] = new
        skipped |= set(range(p.start + 1, p.end + 1))
    out, index = [], 0
    for k in range(len(rp.instructions)):
        n = expansions[k]
        if k in replaced:
            out += replaced[k]
        elif k not in skipped:
            out += rewritten[index : index + n]
        index += n
        if k == insert_after:
            out += list(block)
    return out


def write_page(pdf: pikepdf.Pdf, page_no: int, instructions: list) -> None:
    pdf.pages[page_no].obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))


# Links -----------------------------------------------------------------------------------


@dataclass
class Link:
    index: int  # position in the page's /Annots
    rect: tuple[float, float, float, float]  # PDF user space
    uri: str | None
    single_quad: bool  # no QuadPoints, or one quad: the rect is the whole clickable area
    glyphs: frozenset[int]  # the page's glyphs under it


def page_links(pdf: pikepdf.Pdf, page_no: int, rp: Replay) -> list[Link]:
    annots = pdf.pages[page_no].obj.get("/Annots") or []
    out = []
    for i, a in enumerate(annots):
        try:
            if a.get("/Subtype") != pikepdf.Name.Link or "/Rect" not in a:
                continue
            x0, y0, x1, y1 = (float(v) for v in a.Rect)
        except (AttributeError, TypeError, ValueError):
            continue
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        action = a.get("/A")
        uri = str(action.URI) if action is not None and action.get("/S") == pikepdf.Name.URI and "/URI" in action else None
        quads = a.get("/QuadPoints")
        under = frozenset(
            t for t, g in enumerate(rp.glyphs)
            if x0 - 0.5 <= g.origin[0] + _width(g) / 2 <= x1 + 0.5 and y0 - 1 <= g.origin[1] <= y1 + 1
        )
        out.append(Link(i, (x0, y0, x1, y1), uri, quads is None or len(quads) <= 8, under))
    return out


def _width(g) -> float:
    return max(g.advance / 1000 * g.paint.size * g.paint.Tz * g.paint.trm2[0], 0.0)


@dataclass
class LinkChange:
    link: Link
    rect: tuple[float, float, float, float] | None  # None: remove the link
    uri: str | None = None  # a new target, or None to keep it


def new_target(uri: str | None, old_text: str, new_text: str) -> str | None:
    """A link whose target is its own text ("https://…" shown as itself, "mailto:" plus
    the address shown) gets the new text. None: the target isn't its text."""
    # The clickable area often takes in the full stop or bracket after the address.
    edges = ".,;:!?()[]{}<>«»\"'“”‘’"
    old_text, new_text = old_text.strip(edges), new_text.strip(edges)
    if not uri or not old_text or not new_text:
        return None
    for prefix in ("", "mailto:", "http://", "https://"):
        for slash in ("", "/"):
            if uri == prefix + old_text + slash:
                return prefix + new_text + slash
    return None


def apply_links(pdf: pikepdf.Pdf, page_no: int, changes: list[LinkChange]) -> None:
    if not changes:
        return
    page = pdf.pages[page_no].obj
    annots = page.Annots
    removed = sorted((c.link.index for c in changes if c.rect is None), reverse=True)
    for c in changes:
        if c.rect is None:
            continue
        a = annots[c.link.index]
        x0, y0, x1, y1 = c.rect
        a.Rect = pikepdf.Array([x0, y0, x1, y1])
        if "/QuadPoints" in a:
            a.QuadPoints = pikepdf.Array([x0, y1, x1, y1, x0, y0, x1, y0])
        if c.uri is not None:
            a.A.URI = pikepdf.String(c.uri)
    for i in removed:
        del annots[i]


# Checks ----------------------------------------------------------------------------------


def render(doc_bytes: bytes, page_no: int, zoom: float = 2.0):
    with pymupdf.open("pdf", doc_bytes) as d:
        pix = d[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csGRAY)
        mu = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()
    doc = pdfium.PdfDocument(doc_bytes)
    try:
        bm = doc[page_no].render(scale=zoom, grayscale=True)
        pf = bm.to_numpy().reshape(bm.height, bm.width).copy()
    finally:
        doc.close()
    return mu, pf


def outside(a, b, boxes, zoom, tolerance_px):
    """Pixels that differ outside the boxes (displayed page space)."""
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:h, :w].astype(int), b[:h, :w].astype(int)
    mask = np.ones((h, w), bool)
    for box in boxes:
        x0, y0, x1, y1 = (v * zoom for v in box)
        mask[max(0, int(y0)) : int(y1) + 1, max(0, int(x0)) : int(x1) + 1] = False
    if tolerance_px == 0:
        changed = a != b
    else:
        def spread(img, fn):
            out = img.copy()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    out = fn(out, np.roll(np.roll(img, dy, 0), dx, 1))
            return out
        changed = (b < spread(a, np.minimum)) | (b > spread(a, np.maximum)) | (a < spread(b, np.minimum)) | (a > spread(b, np.maximum))
    return int(changed[mask].sum())


def ink(img, box, zoom) -> int:
    """Pixels in the box clearly unlike their background: darker, or lighter (white
    text in a dark table header)."""
    x0, y0, x1, y1 = (int(v * zoom) for v in box)
    region = img[max(0, y0) : y1 + 1, max(0, x0) : x1 + 1].astype(int)
    if region.size == 0:
        return 0
    return int((abs(region - np.median(region)) > 60).sum())


def ink_box(rp: Replay, traces) -> tuple[float, float, float, float]:
    """Where these glyphs can leave ink (PDF space), from their own size and text matrix:
    from below the descent to above the ascent, with room for overhangs. MuPDF's boxes
    can be smaller: a glyph squeezed to half width is reported at half its height."""
    xs, ys = [], []
    for t in traces:
        g = rp.glyphs[t]
        p = g.paint
        a, b, c, d = p.trm2
        width = g.advance / 1000 * p.Tz
        for u, v in ((-0.3, -0.4), (width + 0.3, -0.4), (-0.3, 1.2), (width + 0.3, 1.2)):
            xs.append(g.origin[0] + p.size * (u * a + v * c))
            ys.append(g.origin[1] + p.size * (u * b + v * d))
    return (min(xs), min(ys), max(xs), max(ys))


def to_display(page: pymupdf.Page, bbox) -> pymupdf.Rect:
    """PDF user space -> displayed page space (what renders and readers use)."""
    r = pymupdf.Rect(bbox) * page_matrix(page) * page.rotation_matrix
    r.normalize()
    return r


def to_pdf(page: pymupdf.Page, rect) -> tuple[float, float, float, float]:
    r = pymupdf.Rect(rect) * (page.derotation_matrix * ~page_matrix(page))
    r.normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def _round(color):
    return tuple(round(c, 3) for c in color) if color else ()


def _drawings(page: pymupdf.Page) -> list[tuple[tuple, tuple]]:
    """MuPDF's own list of the page's shapes: (look, rect in PDF space). MuPDF gives
    shapes in unrotated space (links, on the other hand, as displayed)."""
    out = []
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"]) * ~page_matrix(page)
        r.normalize()
        look = (d["type"], _round(d.get("fill")), _round(d.get("color")), round(d.get("width") or 0, 2))
        out.append((look, (r.x0, r.y0, r.x1, r.y1)))
    return out


def _same_rect(a, b, tolerance=0.15) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def check_drawings(old_page: pymupdf.Page, new_page: pymupdf.Page, changes: list[PathChange], rp: Replay) -> None:
    """MuPDF sees exactly the same shapes as before, except the decorations changed:
    each gone, or at its new span."""
    expected = _drawings(old_page)
    for c in changes:
        bbox = rp.paths[c.path].bbox
        k = next((i for i, (_, r) in enumerate(expected) if _same_rect(r, bbox)), None)
        if k is None:
            fail("an underline couldn't be found")
        look, (x0, y0, x1, y1) = expected.pop(k)
        for a, b in c.spans:
            s = (b - a) / (c.old[1] - c.old[0])
            expected.append((look, (a + (x0 - c.old[0]) * s, y0, a + (x1 - c.old[0]) * s, y1)))
    actual = _drawings(new_page)
    if len(actual) != len(expected):
        fail("the page's lines and shapes changed")
    unused = list(actual)
    for look, rect in expected:
        k = next((i for i, (l, r) in enumerate(unused) if l == look and _same_rect(r, rect)), None)
        if k is None:
            fail("the page's lines and shapes changed")
        unused.pop(k)


def _links(page: pymupdf.Page) -> list[tuple[tuple, str | None]]:
    return [(to_pdf(page, l["from"]), l.get("uri")) for l in page.get_links()]


def check_links(old_page: pymupdf.Page, new_page: pymupdf.Page, changes: list[LinkChange]) -> None:
    """MuPDF reads the links as planned: the same, except those moved, retargeted or removed."""
    expected = _links(old_page)
    for c in changes:
        k = next((i for i, (r, _) in enumerate(expected) if _same_rect(r, c.link.rect, 0.05)), None)
        if k is None:
            fail("a link couldn't be found")
        rect, uri = expected.pop(k)
        if c.rect is not None:
            expected.append((c.rect, c.uri if c.uri is not None else uri))
    actual = _links(new_page)
    if len(actual) != len(expected):
        fail("the page's links changed")
    unused = list(actual)
    for rect, uri in expected:
        k = next((i for i, (r, u) in enumerate(unused) if u == uri and _same_rect(r, rect, 0.05)), None)
        if k is None:
            fail("the page's links changed")
        unused.pop(k)


def contents(page) -> bytes:
    data = page.obj.get("/Contents")
    if data is None:
        return b""
    if isinstance(data, pikepdf.Array):
        return b" ".join(s.read_bytes() for s in data)
    return data.read_bytes()


def check_other_pages(old_bytes: bytes, new_bytes: bytes, changed: set[int]) -> None:
    old_pdf, new_pdf = pikepdf.open(io.BytesIO(old_bytes)), pikepdf.open(io.BytesIO(new_bytes))
    if len(old_pdf.pages) != len(new_pdf.pages):
        fail("the number of pages changed")
    for i, (a, b) in enumerate(zip(old_pdf.pages, new_pdf.pages)):
        if i not in changed and contents(a) != contents(b):
            fail("another page changed")
