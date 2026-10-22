"""Delete whole lines, on one page or several, as one change.

A line's glyphs are taken out of the page's instructions (each replaced by an equal
spacing number, so nothing else moves), together with its underlines, strikethroughs
and highlights, and the links that only cover deleted text. The lines below don't move
up: the space stays, as in other PDF editors.

Deleting needs no fonts and no layout, so it works on lines that can't be edited
(rotated text, unreadable letters, fonts not on the server). Before it is accepted:
every other glyph on the page is drawn exactly as before, nothing outside the deleted
lines changes by a single pixel (MuPDF and PDFium), PDFium reads no letters left where
the lines were, and MuPDF sees the same shapes and links minus those removed.
"""

import io

import pikepdf
import pymupdf
import pypdfium2 as pdfium

from . import decorations
from .changes import (
    EditRejected,
    LinkChange,
    PathChange,
    apply_links,
    check_drawings,
    check_links,
    check_other_pages,
    fail,
    ink_box,
    outside,
    page_links,
    render,
    rewrite,
    to_display,
    write_page,
)
from .editing import EditResult, comparable
from .pdfcontent import Unsupported, matches_trace, replay
from .textlayout import Line, page_lines


def delete_lines(src: bytes, targets: list[tuple[int, str, str]]) -> EditResult:
    """targets: (page index, line id, the line's text as the user saw it)."""
    mu = pymupdf.open("pdf", src)
    pdf = pikepdf.open(io.BytesIO(src))
    by_page: dict[int, list[tuple[str, str]]] = {}
    for page_no, line_id, old_text in targets:
        if not 0 <= page_no < mu.page_count:
            raise EditRejected("That page doesn't exist.")
        by_page.setdefault(page_no, []).append((line_id, old_text))
    if not by_page:
        raise EditRejected("No lines were chosen.")

    warnings: list[str] = []
    plans = {}
    for page_no, items in sorted(by_page.items()):
        page = mu[page_no]
        lines = page_lines(page)
        chosen: list[Line] = []
        for line_id, old_text in items:
            line = next((l for l in lines if l.id == line_id), None)
            if line is None or comparable(line.text) != comparable(old_text):
                raise EditRejected("A line changed since you selected it. Select it again.")
            if all(l.id != line.id for l in chosen):
                chosen.append(line)
        try:
            rp = replay(pdf, page_no)
        except Unsupported as e:
            raise EditRejected(str(e))
        if not matches_trace(rp, page):
            raise EditRejected("This page is built in a way that can't be edited safely yet.")

        remove = {t for l in chosen for t in l.members}
        path_changes: dict[int, PathChange] = {}
        for l in chosen:
            for d in decorations.for_line(l, rp):
                if d.traces <= remove and d.path not in path_changes:
                    if d.shared:
                        raise EditRejected(
                            "An underline on this line is drawn together with other shapes, so it can't be removed safely yet."
                        )
                    path_changes[d.path] = PathChange(d.path, (d.x0, d.x1), [])
        link_changes = []
        for link in page_links(pdf, page_no, rp):
            if not link.glyphs & remove:
                continue
            if link.glyphs <= remove:
                link_changes.append(LinkChange(link, None))
            else:
                warnings.append("A link that also covers other text was kept.")
        write_page(pdf, page_no, rewrite(rp, remove, paths=list(path_changes.values())))
        apply_links(pdf, page_no, link_changes)
        plans[page_no] = (chosen, lines, rp, remove, list(path_changes.values()), link_changes)

    out = io.BytesIO()
    pdf.save(out)
    result = out.getvalue()
    _verify(src, result, plans)
    if _left_behind(result, plans):
        warnings.append("Something drawn under the deleted text (a line or a colored box) is still there. Check the page.")
    return EditResult(result, list(dict.fromkeys(warnings)), "")


def _left_behind(new_bytes: bytes, plans: dict) -> bool:
    """A shape right under or over a deleted line that touches no other text: perhaps
    an underline or highlight not recognized as the text's own. Only a warning: it may
    belong to the page (a rule under a full-width line)."""
    doc = pymupdf.open("pdf", new_bytes)
    for page_no, (chosen, lines, *_rest) in plans.items():
        page = doc[page_no]
        chosen_ids = {l.id for l in chosen}
        remaining = [pymupdf.Rect(l.bbox) for l in lines if l.id not in chosen_ids]
        for d in page.get_drawings():
            # MuPDF gives shapes unrotated; line boxes are as displayed.
            r = pymupdf.Rect(d["rect"]) * page.rotation_matrix
            r.normalize()
            if r.width < 2.5 and r.height > 4:
                continue  # a table's vertical border
            for l in chosen:
                box = pymupdf.Rect(l.bbox)
                size = l.style.size
                # Tight above (the footnote separator sits just above its text), roomier below.
                band = pymupdf.Rect(box.x0 - 1, box.y0 - 0.15 * size, box.x1 + 1, box.y1 + 0.3 * size)
                if band.contains(r) and not any(r.intersects(o) for o in remaining):
                    return True
    return False


def _verify(old_bytes: bytes, new_bytes: bytes, plans: dict) -> None:
    old_doc, new_doc = pymupdf.open("pdf", old_bytes), pymupdf.open("pdf", new_bytes)
    new_pdf = pikepdf.open(io.BytesIO(new_bytes))
    for page_no, (chosen, lines, rp, remove, path_changes, link_changes) in plans.items():
        old_page, new_page = old_doc[page_no], new_doc[page_no]
        try:
            new_rp = replay(new_pdf, page_no)
        except Unsupported:
            fail("the edited page can't be read back")
        if not matches_trace(new_rp, new_page):
            fail("the edited page can't be read back")

        # Every other glyph is drawn exactly as before.
        kept = [g for t, g in enumerate(rp.glyphs) if t not in remove]
        if len(kept) != len(new_rp.glyphs):
            fail("other text on the page would have changed")
        for a, b in zip(kept, new_rp.glyphs):
            if (
                a.code != b.code
                or abs(a.origin[0] - b.origin[0]) > 0.01
                or abs(a.origin[1] - b.origin[1]) > 0.01
                or (a.paint.font, a.paint.size, a.paint.fill) != (b.paint.font, b.paint.size, b.paint.fill)
            ):
                fail("other text on the page would have changed")

        # Nothing outside the deleted lines (and their decorations) changes, in two engines.
        # Vowel marks can reach a little past the font's height: a third of the size.
        # And the glyphs' own size and text matrix: MuPDF's box can be too small.
        boxes = [pymupdf.Rect(l.bbox) + _margin(l) for l in chosen]
        boxes += [to_display(old_page, ink_box(rp, l.members)) + (-1, -1, 1, 1) for l in chosen]
        boxes += [to_display(old_page, rp.paths[c.path].bbox) + (-3, -3, 3, 3) for c in path_changes]
        old_mu, old_pf = render(old_bytes, page_no)
        new_mu, new_pf = render(new_bytes, page_no)
        if outside(old_mu, new_mu, [tuple(b) for b in boxes], 2.0, 0) or outside(old_pf, new_pf, [tuple(b) for b in boxes], 2.0, 1):
            fail("other parts of the page would have moved")

        # PDFium reads no letters where only deleted lines were.
        deleted = [pymupdf.Rect(l.bbox) for l in chosen]
        chosen_ids = {l.id for l in chosen}
        remaining = [pymupdf.Rect(l.bbox) + (-1, -1, 1, 1) for l in lines if l.id not in chosen_ids]
        doc = pdfium.PdfDocument(new_bytes)
        try:
            tp = doc[page_no].get_textpage()
            for i in range(tp.count_chars()):
                if not tp.get_text_range(i, 1).isalnum():
                    continue
                left, bottom, right, top = tp.get_charbox(i)
                centre = to_display(old_page, (left, bottom, right, top))
                point = pymupdf.Point((centre.x0 + centre.x1) / 2, (centre.y0 + centre.y1) / 2)
                if any(point in r for r in deleted) and not any(point in r for r in remaining):
                    fail("a second PDF reader still saw the deleted text")
        finally:
            doc.close()

        check_drawings(old_page, new_page, path_changes, rp)
        check_links(old_page, new_page, link_changes)
    check_other_pages(old_bytes, new_bytes, set(plans))


def _margin(line: Line) -> tuple[float, float, float, float]:
    v = max(3.0, 0.35 * line.style.size)
    return (-3.0, -v, 3.0, v)
