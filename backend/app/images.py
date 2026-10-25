"""Pictures on a page: list them, delete one, replace one, add one, move or resize one.

A picture is drawn by one instruction (Do, for an image or a form without text, or an
inline image): the current transformation (CTM) puts it in a unit square. Every change
touches only that instruction, with a transformation around it, so the rest of the page
is drawn exactly as before. Before a change is accepted: every glyph and shape is where it
was, every other picture too, nothing outside the picture's old and new places changes by
a pixel (MuPDF and PDFium), and no other page changes, except where a picture is replaced
on every page it's on.

New pictures (uploaded) are kept at print quality: at most 300 dots per inch at the size
they're shown, so a phone photo used as a small logo doesn't make the file huge. JPEG
photos keep their own compression; other pictures, their see-through parts too.
"""

import io
import math
import zlib
from dataclasses import dataclass

import pikepdf
import pymupdf
from PIL import Image, ImageOps

from .changes import EditRejected, check_other_pages, fail, outside, render, to_display, to_pdf, write_page
from .editing import EditResult
from .pdfcontent import IDENTITY, Matrix, Replay, Unsupported, inverse, matches_trace, mul, page_matrix, replay

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
DPI = 300


@dataclass
class PageImage:
    id: str
    index: int  # into Replay.images
    bbox: tuple[float, float, float, float]  # as displayed
    kind: str  # "image", "inline" or "drawing" (a form: a logo made of shapes, say)
    name: str | None  # the XObject's name on the page
    width: int  # pixels (0 for a drawing)
    height: int
    pages: int  # pages the same picture is drawn on
    background: bool  # covers most of the page

    def json(self) -> dict:
        return {
            "id": self.id, "bbox": [round(v, 2) for v in self.bbox], "kind": self.kind,
            "width": self.width, "height": self.height, "pages": self.pages, "background": self.background,
        }


def _xobjects(pdf: pikepdf.Pdf, page_no: int):
    res = pdf.pages[page_no].obj.get("/Resources") or pdf.pages[page_no].Resources
    return res.get("/XObject") or {}


def _uses(pdf: pikepdf.Pdf, replays: dict | None = None) -> dict[tuple[int, int], set[int]]:
    """For each image or form object: the pages that draw it."""
    out: dict[tuple[int, int], set[int]] = {}
    for n in range(len(pdf.pages)):
        if replays is not None:
            rp = replays.get(n)
            if rp is None:
                continue
        else:
            try:
                rp = replay(pdf, n)
            except Unsupported:
                continue
        xo = _xobjects(pdf, n)
        for im in rp.images:
            ins = rp.instructions[im.op]
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                continue
            obj = xo.get(str(ins.operands[0]))
            if obj is not None and obj.is_indirect:
                out.setdefault(obj.objgen, set()).add(n)
    return out


def page_images(src: bytes, page_no: int) -> list[PageImage]:
    """The pictures on a page, in drawing order."""
    doc = pymupdf.open("pdf", src)
    with pikepdf.open(io.BytesIO(src)) as pdf:
        try:
            rp = replay(pdf, page_no)
        except Unsupported:
            return []
        return _listing(doc[page_no], pdf, page_no, rp, _uses(pdf))


def all_page_images(src: bytes) -> list[list[PageImage]]:
    """The pictures on every page: each page read once (for a whole-file download)."""
    doc = pymupdf.open("pdf", src)
    with pikepdf.open(io.BytesIO(src)) as pdf:
        replays = {}
        for n in range(len(pdf.pages)):
            try:
                replays[n] = replay(pdf, n)
            except Unsupported:
                pass
        uses = _uses(pdf, replays)
        return [_listing(doc[n], pdf, n, replays[n], uses) if n in replays else [] for n in range(len(pdf.pages))]


def _listing(page: pymupdf.Page, pdf: pikepdf.Pdf, page_no: int, rp: Replay, uses) -> list[PageImage]:
    xo = _xobjects(pdf, page_no)
    area = page.rect.width * page.rect.height
    out = []
    for k, im in enumerate(rp.images):
        ins = rp.instructions[im.op]
        box = tuple(to_display(page, im.bbox) & page.rect)
        if pymupdf.Rect(box).is_empty:
            continue
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            kind, name, w, h, pages = "inline", None, 0, 0, 1
        else:
            name = str(ins.operands[0])
            obj = xo.get(name)
            if obj is None:
                continue
            kind = "image" if obj.get("/Subtype") == pikepdf.Name.Image else "drawing"
            w, h = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
            pages = len(uses.get(obj.objgen, {page_no})) if obj.is_indirect else 1
        r = pymupdf.Rect(box)
        out.append(PageImage(f"{page_no}-img-{k}", k, box, kind, name, w, h, pages, r.width * r.height > 0.6 * area))
    return out


_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
_SHOW = {"Tj", "TJ", "'", '"'}


def picture_png(src: bytes | pikepdf.Pdf, page_no: int, image_id: str, width: int) -> bytes:
    """One picture alone, on a see-through background, cut to its place: what is shown
    being dragged while it's moved. Everything else on the page is left out, but the
    page's clipping and colors stay, so the picture looks as it does on the page.
    `src` may be an open file (for many pictures of one file): it isn't changed."""
    whole = src if isinstance(src, pikepdf.Pdf) else pikepdf.open(io.BytesIO(src))
    if not 0 <= page_no < len(whole.pages):
        raise EditRejected("That page doesn't exist.")
    # A copy of just this page, to change.
    pdf = pikepdf.new()
    pdf.pages.append(whole.pages[page_no])
    try:
        rp = replay(pdf, 0)
    except Unsupported as e:
        raise EditRejected(str(e))
    k = _index(image_id)
    if not 0 <= k < len(rp.images):
        raise EditRejected("This picture changed since you selected it. Select it again.")
    keep = rp.images[k].op
    out = []
    for i, ins in enumerate(rp.instructions):
        if i != keep:
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                continue
            op = str(ins.operator)
            if op in _SHOW or op in ("Do", "sh"):
                continue
            if op in _PAINT:  # the path still clips, if it does, but isn't drawn
                ins = _op([], "n")
        out.append(ins)
    write_page(pdf, 0, out)
    page = pymupdf.open("pdf", _save(pdf))[0]
    area = to_display(page, rp.images[k].bbox) & page.rect
    if area.is_empty:
        raise EditRejected("This picture isn't on the page.")
    zoom = max(200, min(2000, width)) / area.width
    zoom = min(zoom, (4_000_000 / (area.width * area.height)) ** 0.5)
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=area, alpha=True, annots=False).tobytes("png")


# Uploaded pictures -----------------------------------------------------------------------


def _picture(data: bytes, size_pt: tuple[float, float]):
    """An uploaded picture, as a PDF image (and its see-through part), at most 300 dpi at
    `size_pt`. Returns (image stream maker, pixel width, pixel height)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise EditRejected(f"This picture is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        img = Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP"])  # nothing else is read
        fmt = img.format
        if img.width * img.height > MAX_PIXELS:
            raise EditRejected("This picture has too many pixels. Use a smaller one.")
        turned = img.getexif().get(0x0112, 1) not in (0, 1)  # a phone photo taken sideways
        img = ImageOps.exif_transpose(img)  # the right way up
    except EditRejected:
        raise
    except Exception:
        raise EditRejected("That file isn't a picture this editor can use. Use a JPEG or PNG.")
    # At most 300 dots per inch at the size it's shown.
    w_in, h_in = max(size_pt[0], 1) / 72, max(size_pt[1], 1) / 72
    scale = min(1.0, DPI * w_in / img.width, DPI * h_in / img.height)
    resized = scale < 0.999
    if resized:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)

    def make(pdf: pikepdf.Pdf) -> pikepdf.Stream:
        if fmt == "JPEG" and not resized and not turned and img.mode in ("RGB", "L"):
            stream = pikepdf.Stream(pdf, data)
            stream.Filter = pikepdf.Name.DCTDecode
            colors = pikepdf.Name.DeviceRGB if img.mode == "RGB" else pikepdf.Name.DeviceGray
        else:
            alpha = None
            if img.mode in ("RGBA", "LA", "P", "PA") or "transparency" in img.info:
                rgba = img.convert("RGBA")
                alpha = rgba.getchannel("A")
                base = rgba.convert("RGB")
            else:
                base = img.convert("L" if img.mode in ("L", "1", "I", "I;16") else "RGB")
            colors = pikepdf.Name.DeviceRGB if base.mode == "RGB" else pikepdf.Name.DeviceGray
            stream = pikepdf.Stream(pdf, zlib.compress(base.tobytes()))
            stream.Filter = pikepdf.Name.FlateDecode
            if alpha is not None and alpha.getextrema() != (255, 255):
                mask = pikepdf.Stream(pdf, zlib.compress(alpha.tobytes()))
                mask.Type, mask.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
                mask.Width, mask.Height = img.width, img.height
                mask.ColorSpace, mask.BitsPerComponent, mask.Filter = pikepdf.Name.DeviceGray, 8, pikepdf.Name.FlateDecode
                stream.SMask = mask
        stream.Type, stream.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        stream.Width, stream.Height = img.width, img.height
        stream.ColorSpace, stream.BitsPerComponent = colors, 8
        return stream

    return make, img.width, img.height


def _new_name(pdf: pikepdf.Pdf, page_no: int, xobject) -> str:
    """A name for a new picture, free on the page."""
    page = pdf.pages[page_no].obj
    if "/Resources" not in page:
        page.Resources = pdf.make_indirect(pikepdf.Dictionary())
    res = page.Resources
    if "/XObject" not in res:
        res.XObject = pikepdf.Dictionary()
    k = 1
    while f"/Pic{k}" in res.XObject:
        k += 1
    res.XObject[f"/Pic{k}"] = xobject
    return f"/Pic{k}"


def _op(operands, op) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(op))


def _scales(ctm: Matrix) -> tuple[float, float]:
    """How wide and how tall the unit square is drawn, in page units."""
    return math.hypot(ctm[0], ctm[1]), math.hypot(ctm[2], ctm[3])


def _fit(ctm: Matrix, w: int, h: int, mode: str) -> tuple[list, bool]:
    """A cm inside the picture's unit square that shows a w x h picture "fit" (all of it,
    centered), "fill" (covering it, cut to it) or "stretch". And whether to cut."""
    box_w, box_h = _scales(ctm)
    box, new = box_w / box_h, w / h
    if mode == "stretch" or abs(box - new) < 1e-6:
        return [1, 0, 0, 1, 0, 0], False
    if (mode == "fit") == (new > box):
        sx, sy = 1.0, box / new
    else:
        sx, sy = new / box, 1.0
    return [sx, 0, 0, sy, (1 - sx) / 2, (1 - sy) / 2], mode == "fill"


# Changes ---------------------------------------------------------------------------------


def _unit(pdf: pikepdf.Pdf, page_no: int, rp: Replay, k: int) -> Matrix:
    """Where a picture's unit square is, in its drawing's space: an image is drawn on it;
    a form (a drawing) on its own box, which a new picture should fill instead."""
    ins = rp.instructions[rp.images[k].op]
    if isinstance(ins, pikepdf.ContentStreamInlineImage):
        return IDENTITY
    obj = _xobjects(pdf, page_no).get(str(ins.operands[0]))
    if obj is None or obj.get("/Subtype") != pikepdf.Name.Form:
        return IDENTITY
    try:
        x0, y0, x1, y1 = (float(v) for v in obj.BBox)
        m = tuple(float(v) for v in obj.get("/Matrix", [1, 0, 0, 1, 0, 0]))
    except (AttributeError, TypeError, ValueError):
        raise EditRejected("This drawing can't be replaced: its size isn't known.")
    if abs(x1 - x0) < 1e-6 or abs(y1 - y0) < 1e-6:
        raise EditRejected("This drawing can't be replaced: it has no size.")
    return mul((abs(x1 - x0), 0.0, 0.0, abs(y1 - y0), min(x0, x1), min(y0, y1)), m)


def _find(rp: Replay, page: pymupdf.Page, index: int, bbox) -> int:
    """The picture the person picked: by its place in the drawing order, and still where
    they saw it."""
    if not 0 <= index < len(rp.images):
        raise EditRejected("This picture changed since you selected it. Select it again.")
    shown = to_display(page, rp.images[index].bbox) & page.rect
    if any(abs(a - b) > 0.5 for a, b in zip(shown, bbox)):
        raise EditRejected("This picture changed since you selected it. Select it again.")
    return index


def _open(src: bytes, page_no: int):
    doc = pymupdf.open("pdf", src)
    if not 0 <= page_no < doc.page_count:
        raise EditRejected("That page doesn't exist.")
    pdf = pikepdf.open(io.BytesIO(src))
    try:
        rp = replay(pdf, page_no)
    except Unsupported as e:
        raise EditRejected(str(e))
    if not matches_trace(rp, doc[page_no]) or rp.stray_restore:
        raise EditRejected("This page is built in a way that can't be edited safely yet.")
    return doc, pdf, rp


def _save(pdf: pikepdf.Pdf) -> bytes:
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _index(image_id: str) -> int:
    try:
        return int(image_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        raise EditRejected("This picture changed since you selected it. Select it again.")


def delete_image(src: bytes, page_no: int, image_id: str, bbox) -> EditResult:
    doc, pdf, rp = _open(src, page_no)
    k = _find(rp, doc[page_no], _index(image_id), bbox)
    op = rp.images[k].op
    write_page(pdf, page_no, [ins for i, ins in enumerate(rp.instructions) if i != op])
    new = _save(pdf)
    area = tuple(to_display(doc[page_no], rp.images[k].bbox) + (-1, -1, 1, 1))
    _verify(src, new, page_no, rp, {k: None}, [area], added=0)
    return EditResult(new, [], "", [(page_no, *area)], [(page_no, *bbox)])


def replace_image(src: bytes, page_no: int, image_id: str, bbox, data: bytes, mode: str = "fit", everywhere: bool = False) -> EditResult:
    """Show a new picture where this one is: "fit" (all of it), "fill" (covering, cut to
    the place) or "stretch". With everywhere, on every page the same picture is on."""
    if mode not in ("fit", "fill", "stretch"):
        raise EditRejected("Choose fit, fill or stretch.")
    doc, pdf, rp = _open(src, page_no)
    k = _find(rp, doc[page_no], _index(image_id), bbox)
    target = rp.images[k]
    w_pt, h_pt = _scales(mul(_unit(pdf, page_no, rp, k), target.ctm))
    make, w, h = _picture(data, (w_pt, h_pt))
    stream = pdf.make_indirect(make(pdf))
    # Which drawings to replace: this one; or every one of the same picture.
    todo: dict[int, list[int]] = {page_no: [k]}
    ins = rp.instructions[target.op]
    if everywhere and not isinstance(ins, pikepdf.ContentStreamInlineImage):
        obj = _xobjects(pdf, page_no).get(str(ins.operands[0]))
        if obj is not None and obj.is_indirect:
            todo = {}
            for n in range(len(pdf.pages)):
                prp = rp if n == page_no else replay(pdf, n)
                xo = _xobjects(pdf, n)
                for j, im in enumerate(prp.images):
                    i2 = prp.instructions[im.op]
                    if not isinstance(i2, pikepdf.ContentStreamInlineImage) and (o := xo.get(str(i2.operands[0]))) is not None and o.is_indirect and o.objgen == obj.objgen:
                        todo.setdefault(n, []).append(j)
    areas, plans = [], {}
    for n, ks in sorted(todo.items()):
        prp = rp if n == page_no else replay(pdf, n)
        name = _new_name(pdf, n, stream)
        swap = {}
        for j in ks:
            im = prp.images[j]
            unit = _unit(pdf, n, prp, j)
            cm, cut = _fit(mul(unit, im.ctm), w, h, mode)
            block = [_op([], "q")]
            if unit != IDENTITY:
                block.append(_op([round(v, 6) for v in unit], "cm"))
            if cut:
                block += [_op([0, 0, 1, 1], "re"), _op([], "W"), _op([], "n")]
            block += [_op(cm, "cm"), _op([pikepdf.Name(name)], "Do"), _op([], "Q")]
            swap[im.op] = block
        out = []
        for i, ins2 in enumerate(prp.instructions):
            out += swap.get(i, [ins2])
        write_page(pdf, n, out)
        page_areas = [tuple(to_display(doc[n], prp.images[j].bbox) + (-1, -1, 1, 1)) for j in ks]
        areas += [(n, *a) for a in page_areas]
        plans[n] = (prp, {j: "replaced" for j in ks}, page_areas)
    new = _save(pdf)
    for n, (prp, changes, page_areas) in plans.items():
        _verify(src, new, n, prp, changes, page_areas, added=0, other_pages=False)
    check_other_pages(src, new, set(plans))
    marks = [(n, *tuple(to_display(doc[n], plans[n][0].images[j].bbox))) for n in plans for j in plans[n][1]]
    return EditResult(new, [], "", areas, marks)


def _box_matrix(page: pymupdf.Page, box) -> Matrix:
    """A CTM that draws the unit square on `box` (as displayed), upright as displayed."""
    x0, y0, x1, y1 = box
    to = page.derotation_matrix * ~page_matrix(page)
    a = pymupdf.Point(x0, y1) * to  # the picture's bottom left, as displayed
    b = pymupdf.Point(x1, y1) * to  # bottom right
    c = pymupdf.Point(x0, y0) * to  # top left
    return (b.x - a.x, b.y - a.y, c.x - a.x, c.y - a.y, a.x, a.y)


def add_image(src: bytes, page_no: int, data: bytes, box) -> EditResult:
    """A new picture on `box` (as displayed), drawn over the page."""
    doc, pdf, rp = _open(src, page_no)
    page = doc[page_no]
    box = tuple(pymupdf.Rect(box) & page.rect)
    if pymupdf.Rect(box).width < 2 or pymupdf.Rect(box).height < 2:
        raise EditRejected("Put the picture on the page.")
    make, _w, _h = _picture(data, (box[2] - box[0], box[3] - box[1]))
    name = _new_name(pdf, page_no, pdf.make_indirect(make(pdf)))
    # At the very end, with the page's own drawing state restored.
    closing = [_op([], "Q")] * (rp.open_states + 1)
    block = [_op([], "q"), _op([round(v, 4) for v in _box_matrix(page, box)], "cm"), _op([pikepdf.Name(name)], "Do"), _op([], "Q")]
    write_page(pdf, page_no, [_op([], "q"), *rp.instructions, *closing, *block])
    new = _save(pdf)
    area = tuple(pymupdf.Rect(box) + (-1, -1, 1, 1))
    _verify(src, new, page_no, rp, {}, [area], added=1)
    return EditResult(new, [], "", [(page_no, *area)], [(page_no, *box)])


def move_image(src: bytes, page_no: int, image_id: str, bbox, box) -> EditResult:
    """Move or resize a picture: from where it is to `box` (as displayed)."""
    doc, pdf, rp = _open(src, page_no)
    page = doc[page_no]
    k = _find(rp, page, _index(image_id), bbox)
    im = rp.images[k]
    old = to_display(page, im.bbox)
    new_box = pymupdf.Rect(box)
    if new_box.width < 2 or new_box.height < 2 or not page.rect.intersects(new_box):
        raise EditRejected("Put the picture on the page.")
    # Old place to new, as displayed, then in the page's own coordinates.
    sx, sy = new_box.width / old.width, new_box.height / old.height
    shown = pymupdf.Matrix(sx, 0, 0, sy, new_box.x0 - old.x0 * sx, new_box.y0 - old.y0 * sy)
    to_shown = page_matrix(page) * page.rotation_matrix
    t = to_shown * shown * ~to_shown  # PDF space to PDF space (applied left to right)
    move = (t.a, t.b, t.c, t.d, t.e, t.f)
    m = mul(mul(im.ctm, move), inverse(im.ctm))  # the same, under the picture's CTM
    m = [round(v, 6) for v in m]
    back = [round(v, 6) for v in inverse(tuple(m))]
    out = []
    for i, ins in enumerate(rp.instructions):
        if i == im.op:
            out += [_op(m, "cm"), ins, _op(back, "cm")]
        else:
            out.append(ins)
    write_page(pdf, page_no, out)
    new = _save(pdf)
    areas = [tuple(old + (-1, -1, 1, 1)), tuple(new_box + (-1, -1, 1, 1))]
    _verify(src, new, page_no, rp, {k: tuple(new_box)}, areas, added=0)
    return EditResult(new, [], "", [(page_no, *a) for a in areas], [(page_no, *tuple(new_box))])


def _verify(old_bytes: bytes, new_bytes: bytes, page_no: int, rp: Replay, changes: dict, areas: list, added: int, other_pages: bool = True) -> None:
    """Every glyph and shape as before; the other pictures as before, the changed ones gone
    (None), moved (their new box, as displayed) or "replaced"; `added` new ones at the end;
    nothing outside `areas` (as displayed) changed by a pixel, in two engines."""
    old_page = pymupdf.open("pdf", old_bytes)[page_no]
    new_doc = pymupdf.open("pdf", new_bytes)
    new_page = new_doc[page_no]
    try:
        new_rp = replay(pikepdf.open(io.BytesIO(new_bytes)), page_no)
    except Unsupported:
        fail("the page can't be read back")
    if not matches_trace(new_rp, new_page):
        fail("the page can't be read back")
    if len(new_rp.glyphs) != len(rp.glyphs) or any(
        a.code != b.code or abs(a.origin[0] - b.origin[0]) > 0.01 or abs(a.origin[1] - b.origin[1]) > 0.01
        for a, b in zip(rp.glyphs, new_rp.glyphs)
    ):
        fail("the text on the page would have changed")
    old_paths = [(p.op, tuple(round(v, 2) for v in p.bbox)) for p in rp.paths]
    new_paths = [(p.op, tuple(round(v, 2) for v in p.bbox)) for p in new_rp.paths]
    clips = [p for p in new_paths if p not in old_paths]
    if [p for p in old_paths if p not in new_paths] or any(op != "n" for op, _ in clips):
        fail("the page's lines and shapes would have changed")
    expected = []
    for k, im in enumerate(rp.images):
        if k in changes and changes[k] is None:
            continue
        expected.append((k, im))
    if len(new_rp.images) != len(expected) + added:
        fail("the page's pictures would have changed")
    for (k, im), got in zip(expected, new_rp.images):
        change = changes.get(k)
        if change == "replaced":
            if not (pymupdf.Rect(im.bbox) + (-0.05, -0.05, 0.05, 0.05)).contains(pymupdf.Rect(got.bbox) & pymupdf.Rect(im.bbox)):
                fail("the new picture wouldn't be in the old one's place")
        elif isinstance(change, tuple):
            if any(abs(a - b) > 0.05 for a, b in zip(to_display(new_page, got.bbox), change)):
                fail("the picture wouldn't be where it was put")
        elif any(abs(a - b) > 0.01 for a, b in zip(im.bbox, got.bbox)):
            fail("another picture would have moved")
    old_mu, old_pf = render(old_bytes, page_no)
    new_mu, new_pf = render(new_bytes, page_no)
    if outside(old_mu, new_mu, areas, 2.0, 0) or outside(old_pf, new_pf, areas, 2.0, 1):
        fail("other parts of the page would have changed")
    if other_pages:
        check_other_pages(old_bytes, new_bytes, {page_no})
