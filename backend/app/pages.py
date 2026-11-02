"""Whole pages: deleting one, adding one, moving one (the page list's X, + and dragging).

Deleting a page takes it out of the document with what points at it: links on other pages
that go to it, bookmarks that open it (their sub-bookmarks that open other pages move up a
level), and form fields that are only on it. Adding a page puts a new one after a page,
like it: the same size and turn, its header and footer drawn again (overflow.py), nothing
else. Moving a page takes the page itself to its new place, so links to it still go to it.

Checked: every other page is the same page as before, with the same drawing, in the order
asked for; the page count is one less or one more. Printed page numbers ("Page 2 of 5")
don't change by themselves: the user is told to check them."""

import io

import pikepdf
import pymupdf

from . import moving, overflow
from .changes import EditRejected, contents, fail
from .editing import EditResult
from .textlayout import page_lines


def _open(src: bytes) -> tuple[pymupdf.Document, pikepdf.Pdf]:
    doc = pymupdf.open("pdf", src)
    pdf = pikepdf.open(io.BytesIO(src))
    return doc, pdf


def _save(pdf: pikepdf.Pdf) -> bytes:
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _numbered(doc: pymupdf.Document) -> bool:
    """Whether the pages' headers or footers have a number in them (a page number)."""
    for n in range(min(doc.page_count, 3)):
        page = doc[n]
        lines = page_lines(page)
        _, footer = moving.bottom_limit(doc, n, lines, (0.0, 0.0, page.rect.width, page.rect.height))
        header = overflow.header_lines(doc, n, lines)
        if any(any(ch.isdigit() for ch in l.text) for l in lines if l.id in footer | header):
            return True
    return False


def _note(doc: pymupdf.Document, what: str = "") -> list[str]:
    if _numbered(doc):
        return [f"{what}Check the page numbers printed on the pages, and the page count: they don't change by themselves."]
    return []


def _goes_to(pdf: pikepdf.Pdf, dest, page) -> bool:
    """Whether a destination (an array, or a name in the document's list of them) opens `page`."""
    if isinstance(dest, (pikepdf.Name, pikepdf.String)):
        names = pdf.Root.get("/Dests")
        found = names.get(dest) if isinstance(names, pikepdf.Dictionary) and isinstance(dest, pikepdf.Name) else None
        if found is None and "/Names" in pdf.Root and "/Dests" in pdf.Root.Names:
            try:
                found = pikepdf.NameTree(pdf.Root.Names.Dests).get(str(dest))
            except Exception:
                found = None
        if isinstance(found, pikepdf.Dictionary):
            found = found.get("/D")
        dest = found
    return isinstance(dest, pikepdf.Array) and len(dest) > 0 and isinstance(dest[0], pikepdf.Dictionary) and dest[0].objgen == page.objgen


def _target(pdf: pikepdf.Pdf, item, page) -> bool:
    """Whether a link or a bookmark opens `page` (its /Dest, or a GoTo action's /D)."""
    if "/Dest" in item and _goes_to(pdf, item.Dest, page):
        return True
    action = item.get("/A")
    return isinstance(action, pikepdf.Dictionary) and action.get("/S") == pikepdf.Name.GoTo and "/D" in action and _goes_to(pdf, action.D, page)


def _bookmarks(pdf: pikepdf.Pdf, page) -> int:
    """Bookmarks that open the page taken out; their sub-bookmarks that open other pages
    move up a level. Returns how many went."""
    gone = 0
    with pdf.open_outline() as outline:

        def keep(items) -> list:
            nonlocal gone
            out = []
            for item in items:
                children = keep(list(item.children))
                if item.obj is not None and _target(pdf, item.obj, page):
                    gone += 1
                    out += children
                else:
                    item.children[:] = children
                    out.append(item)
            return out

        outline.root[:] = keep(list(outline.root))
    return gone


def _fields(pdf: pikepdf.Pdf, page) -> None:
    """Form fields whose boxes are all on the page taken out go from the form; a field
    with boxes on other pages too only loses the ones here."""
    form = pdf.Root.get("/AcroForm")
    if form is None or "/Fields" not in form:
        return
    here = {a.objgen for a in page.get("/Annots") or [] if a.get("/Subtype") == pikepdf.Name.Widget}
    if not here:
        return

    def prune(fields) -> list:
        out = []
        for f in fields:
            if f.objgen in here:
                continue  # a box that's the field itself (merged field and widget)
            if "/Kids" in f:
                kids = prune(list(f.Kids))
                if not kids:
                    continue
                f.Kids = pikepdf.Array(kids)
            out.append(f)
        return out

    form.Fields = pikepdf.Array(prune(list(form.Fields)))


def delete_page(src: bytes, page_no: int) -> EditResult:
    """Page `page_no` taken out of the document, with what points at it."""
    doc, pdf = _open(src)
    if not 0 <= page_no < len(pdf.pages):
        raise EditRejected("That page doesn't exist.")
    if len(pdf.pages) == 1:
        raise EditRejected("A document needs at least one page, so its only page can't be deleted.")
    page = pdf.pages[page_no].obj
    # Links on other pages that open it: gone with it.
    for n, other in enumerate(pdf.pages):
        if n == page_no or "/Annots" not in other.obj:
            continue
        annots = other.obj.Annots
        kept = [a for a in annots if not (a.get("/Subtype") == pikepdf.Name.Link and _target(pdf, a, page))]
        if len(kept) != len(annots):
            other.obj.Annots = pdf.make_indirect(pikepdf.Array(kept))
    if "/Outlines" in pdf.Root:
        _bookmarks(pdf, page)
    _fields(pdf, page)
    if "/OpenAction" in pdf.Root and isinstance(pdf.Root.OpenAction, (pikepdf.Array, pikepdf.Dictionary)):
        action = pdf.Root.OpenAction
        if (isinstance(action, pikepdf.Array) and _goes_to(pdf, action, page)) or (isinstance(action, pikepdf.Dictionary) and _target(pdf, action, page)):
            del pdf.Root["/OpenAction"]
    before = [contents(p) for p in pdf.pages]
    del pdf.pages[page_no]
    new = _save(pdf)
    _check(src, new, [n for n in range(len(before)) if n != page_no])
    return EditResult(new, _note(doc), "", [], [])


def add_page(src: bytes, after: int) -> EditResult:
    """A new page after page `after`, like it: its size and turn, its header and footer."""
    doc, pdf = _open(src)
    if not 0 <= after < len(pdf.pages):
        raise EditRejected("That page doesn't exist.")
    last = len(pdf.pages) - 1
    try:
        if after == last:
            new, notes = overflow.new_page_after(src, after)
        else:
            # Made after the last page, from this page, then moved in place.
            new, notes = _new_page_like(src, after)
            new = move_page(new, last + 1, after + 1, check_only=True).pdf
    except moving.MoveRefused:
        # Its header and footer can't be drawn again: a blank page of its size instead.
        new, notes = _blank_after(src, after), []
    _check(src, new, list(range(after + 1)) + [None] + list(range(after + 1, last + 1)))
    there = pymupdf.open("pdf", new)[after + 1]
    box = (after + 1, 0.0, 0.0, there.rect.width, there.rect.height)
    # (overflow.py's note, said for this page)
    copied = bool(notes) and "header" in notes[0]
    numbered = any("page number" in n for n in notes)
    notes = [f"It has the header and footer of page {after + 1}" + (": check its page number, and the page count on the other pages." if numbered else ".")] if copied else []
    return EditResult(new, notes, "", [box], [box])


def _blank_after(src: bytes, after: int) -> bytes:
    """A blank page after page `after`, its size and turn."""
    pdf = pikepdf.open(io.BytesIO(src))
    model = pdf.pages[after]
    page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=model.mediabox, Resources=pikepdf.Dictionary(), Contents=pdf.make_stream(b""))
    for key in ("/CropBox", "/Rotate"):
        if key in model.obj:
            page[key] = model.obj[key]
    pdf.pages.insert(after + 1, pikepdf.Page(page))
    return _save(pdf)


def _new_page_like(src: bytes, page_no: int) -> tuple[bytes, list[str]]:
    """A new last page like page `page_no` (overflow.new_page_after does it for the last
    page): the page moved to the end for a moment, the new one made after it, moved back."""
    last = len(pikepdf.open(io.BytesIO(src)).pages) - 1
    at_end = move_page(src, page_no, last, check_only=True).pdf
    made, notes = overflow.new_page_after(at_end, last)
    # The model goes back where it was; the new page stays last.
    back = move_page(made, last, page_no, check_only=True).pdf
    return back, notes


def move_page(src: bytes, page_no: int, to: int, check_only: bool = False) -> EditResult:
    """Page `page_no` taken to place `to` (counted after it's taken out: the place it ends
    up at), the other pages keeping their order."""
    doc, pdf = _open(src)
    count = len(pdf.pages)
    if not 0 <= page_no < count or not 0 <= to < count:
        raise EditRejected("That page doesn't exist.")
    if page_no == to:
        raise EditRejected("The page is already there.")
    page = pdf.pages[page_no]
    del pdf.pages[page_no]
    pdf.pages.insert(to, page)
    new = _save(pdf)
    order = [n for n in range(count) if n != page_no]
    order.insert(to, page_no)
    _check(src, new, order)
    if check_only:
        return EditResult(new, [], "", [], [])
    there = pymupdf.open("pdf", new)[to]
    box = (to, 0.0, 0.0, there.rect.width, there.rect.height)
    return EditResult(new, _note(doc, f"Page {page_no + 1} is now page {to + 1}. "), "", [box], [box])


def _check(old: bytes, new: bytes, order: list) -> None:
    """The new file's pages are the old ones in `order` (None: a page that's new), each
    drawn exactly as before, with the same size and turn."""
    a, b = pikepdf.open(io.BytesIO(old)), pikepdf.open(io.BytesIO(new))
    if len(b.pages) != len(order):
        fail("the document would have the wrong number of pages")
    for k, n in enumerate(order):
        if n is None:
            continue
        x, y = a.pages[n], b.pages[k]
        if contents(x) != contents(y):
            fail("a page would have changed")
        if [float(v) for v in x.mediabox] != [float(v) for v in y.mediabox] or x.obj.get("/Rotate", 0) != y.obj.get("/Rotate", 0):
            fail("a page would have changed size")
    da, db = pymupdf.open("pdf", old), pymupdf.open("pdf", new)
    for k, n in enumerate(order):
        if n is not None and da[n].get_text("text") != db[k].get_text("text"):
            fail("a page's text would have changed")
