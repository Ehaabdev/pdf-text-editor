"""Measuring tools for edit accuracy.

An edit is invisible only when:
  * the text of every other line is unchanged (compared by position, not order),
  * the edited line has the expected text, font, size, color and baseline,
  * no pixel outside the edited line changes, in MuPDF and in PDFium (Chrome's engine),
  * links on the page survive.
"""

from functools import lru_cache

import numpy as np
import pymupdf
import pypdfium2 as pdfium

ZOOM = 3


def lines_by_position(page: pymupdf.Page) -> dict[tuple[float, float], str]:
    """Visible text lines keyed by the origin of their first character."""
    out = {}
    for block in page.get_text("rawdict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            chars = [c for s in line["spans"] for c in s["chars"]]
            text = "".join(c["c"] for c in chars).rstrip()
            ink = [c for c in chars if not c["c"].isspace()]
            if not ink:
                continue
            x, y = ink[0]["origin"]
            out[(round(y, 1), round(x, 1))] = text
    return out


def changed_lines(before: pymupdf.Page, after: pymupdf.Page) -> list[tuple[str | None, str | None]]:
    a, b = lines_by_position(before), lines_by_position(after)
    return [(a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def find_line(page: pymupdf.Page, needle: str) -> dict:
    for block in page.get_text("rawdict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            if needle in "".join(c["c"] for s in line["spans"] for c in s["chars"]):
                return line
    raise KeyError(needle)


def line_text(line: dict) -> str:
    return "".join(c["c"] for s in line["spans"] for c in s["chars"]).rstrip()


def ink_chars(line: dict) -> list[dict]:
    return [c for s in line["spans"] for c in s["chars"] if not c["c"].isspace()]


def render_mupdf(pdf: bytes, pno: int) -> np.ndarray:
    with pymupdf.open("pdf", pdf) as doc:
        pix = doc[pno].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False, colorspace=pymupdf.csGRAY)
        return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()


def render_pdfium(pdf: bytes, pno: int) -> np.ndarray:
    doc = pdfium.PdfDocument(pdf)
    try:
        bitmap = doc[pno].render(scale=ZOOM, grayscale=True)
        return bitmap.to_numpy().reshape(bitmap.height, bitmap.width).copy()
    finally:
        doc.close()


def _neighbourhood(img: np.ndarray, radius: int) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = img.copy(), img.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            shifted = np.roll(np.roll(img, dy, 0), dx, 1)
            lo, hi = np.minimum(lo, shifted), np.maximum(hi, shifted)
    return lo, hi


def pixels_changed_outside(
    a: np.ndarray, b: np.ndarray, rect: pymupdf.Rect, pad: float = 2, shift_px: int = 0
) -> int:
    """Pixels outside `rect` that differ between two renders.

    With shift_px=1, a pixel only counts if no pixel within one step of it in the
    other render has its value. PDFium snaps glyphs to whole pixels, so a position
    change of 0.00003pt (float rounding when MuPDF rewrites a page) can move a
    glyph by one pixel; that is not a visible change and should not count.
    """
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:h, :w].astype(int), b[:h, :w].astype(int)
    mask = np.ones((h, w), bool)
    r = (rect + (-pad, -pad, pad, pad)) * ZOOM
    mask[max(0, int(r.y0)) : int(r.y1) + 1, max(0, int(r.x0)) : int(r.x1) + 1] = False
    if shift_px == 0:
        return int((a != b)[mask].sum())
    a_lo, a_hi = _neighbourhood(a, shift_px)
    b_lo, b_hi = _neighbourhood(b, shift_px)
    changed = (b < a_lo) | (b > a_hi) | (a < b_lo) | (a > b_hi)
    return int(changed[mask].sum())


def ink_agreement(a: np.ndarray, b: np.ndarray, rect: pymupdf.Rect, slack_px: int = 1) -> float:
    """How well two renders of the same area agree on where the ink is (1.0 = identical).

    Different engines may place glyphs up to a pixel apart at this zoom (about 0.3pt),
    which is invisible; the best overlap within `slack_px` is taken. A wrong font or
    missing glyphs still scores far lower.
    """
    r = rect * ZOOM
    ys, xs = slice(int(r.y0), int(r.y1) + 1), slice(int(r.x0), int(r.x1) + 1)
    # Engines turn colored backgrounds into slightly different greys, so ink is
    # "much darker than this render's own background", not a fixed threshold.
    ra, rb = a[ys, xs], b[ys, xs]
    ia, ib = ra < np.median(ra) * 0.5, rb < np.median(rb) * 0.5
    best = 0.0
    for dy in range(-slack_px, slack_px + 1):
        for dx in range(-slack_px, slack_px + 1):
            shifted = np.roll(np.roll(ib, dy, 0), dx, 1)
            union = (ia | shifted).sum()
            best = max(best, float((ia & shifted).sum() / union) if union else 1.0)
    return best


@lru_cache(maxsize=4096)
def _has_ink(font_buffer: bytes, ch: str) -> bool:
    doc = pymupdf.open()
    page = doc.new_page(width=60, height=60)
    tw = pymupdf.TextWriter(page.rect)
    tw.append((10, 45), ch, font=pymupdf.Font(fontbuffer=font_buffer), fontsize=40)
    tw.write_text(page)
    pix = page.get_pixmap(alpha=False, colorspace=pymupdf.csGRAY)
    return min(pix.samples) < 200


def undrawable_chars(font_buffer: bytes, text: str) -> list[str]:
    """Characters the font can't draw. A subset font may still list a character
    whose outline was removed, so check that the glyph actually leaves ink."""
    font = pymupdf.Font(fontbuffer=font_buffer)
    missing = []
    for ch in dict.fromkeys(text):
        if ch.isspace():
            continue
        if not font.has_glyph(ord(ch)) or not _has_ink(font_buffer, ch):
            missing.append(ch)
    return missing
